"""Poll the GitHub Integration Service for deliveries.

This deliberately separate self-hosted process connects outward to
api.diffuse.website, verifies the per-instance signature on every delivery,
and feeds the existing local workflow queue. The integration service never receives
the repository clone, index, findings, model credentials, or review output.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from diffuse.api.app import (
    ACCEPTED_ACTIONS,
    _is_github_managed_description_update,
    _record_not_onboarded,
    enqueue_pull_request,
    enqueue_repository_push,
    enqueue_review_conversation,
    record_review_feedback,
)
from diffuse.github.api import (
    fetch_manual_pull_request_event,
    normalize_manual_review_request,
    normalize_pull_request_event,
    normalize_push_event,
    normalize_review_conversation_event,
    normalize_review_feedback_comment_event,
)
from diffuse.repository.registry import RepositoryIdentityConflictError
from diffuse.repository.scm import normalize_base_url, scm_api_timeout_seconds
from diffuse.review.workflow import (
    DeliveryConflictError,
    EventOrderConflictError,
    RepositoryNotOnboardedError,
)

LOGGER = logging.getLogger(__name__)

GITHUB_INTEGRATION_URL_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_URL"
GITHUB_INTEGRATION_TOKEN_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_TOKEN"
GITHUB_DELIVERY_SIGNING_KEY_VARIABLE = "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY"
GITHUB_DELIVERY_POLL_SECONDS_VARIABLE = "DIFFUSE_GITHUB_DELIVERY_POLL_SECONDS"


class GitHubDeliveryPollerError(RuntimeError):
    """A GitHub delivery could not be safely consumed."""


@dataclass(frozen=True)
class DeliveryPollerConfiguration:
    url: str
    instance_token: str
    event_signing_key: str
    poll_seconds: float


def configuration() -> DeliveryPollerConfiguration | None:
    url = os.environ.get(GITHUB_INTEGRATION_URL_VARIABLE, "").strip().rstrip("/")
    token = os.environ.get(GITHUB_INTEGRATION_TOKEN_VARIABLE, "").strip()
    event_key = os.environ.get(GITHUB_DELIVERY_SIGNING_KEY_VARIABLE, "").strip()
    if not any((url, token, event_key)):
        return None
    if not all((url, token, event_key)):
        raise ValueError(
            "GitHub delivery poller configuration is partial; set "
            f"{GITHUB_INTEGRATION_URL_VARIABLE}, {GITHUB_INTEGRATION_TOKEN_VARIABLE}, and "
            f"{GITHUB_DELIVERY_SIGNING_KEY_VARIABLE}."
        )
    # normalize_base_url enforces the repository-wide origin policy: https
    # everywhere, with plain http accepted only for loopback hosts (or under an
    # explicit DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1), so a locally running relay
    # can drive the poll loop end to end without a tunnel.
    url = normalize_base_url(url, field_name=GITHUB_INTEGRATION_URL_VARIABLE)
    poll_seconds = float(os.environ.get(GITHUB_DELIVERY_POLL_SECONDS_VARIABLE, "15"))
    if not 1 <= poll_seconds <= 300:
        raise ValueError(f"{GITHUB_DELIVERY_POLL_SECONDS_VARIABLE} must be between 1 and 300")
    return DeliveryPollerConfiguration(url, token, event_key, poll_seconds)


def _headers(config: DeliveryPollerConfiguration) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.instance_token}"}


def _canonical_event(delivery_id: str, event_name: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {"delivery": delivery_id, "event": event_name, "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _verify_event(
    config: DeliveryPollerConfiguration,
    *,
    delivery_id: str,
    event_name: str,
    payload: dict[str, Any],
    signature: str,
) -> None:
    expected = hmac.new(
        config.event_signing_key.encode(),
        _canonical_event(delivery_id, event_name, payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise GitHubDeliveryPollerError("GitHub delivery signature is invalid")


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _ingest_push(*, delivery_id: str, payload: dict[str, Any], body: bytes) -> str:
    event = normalize_push_event(payload, delivery_id=delivery_id)
    if event is None:
        return "ignored:push_not_default_branch"
    try:
        result = enqueue_repository_push(event, body)
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="push")
        return "ignored:repository_not_onboarded"
    except (
        DeliveryConflictError,
        EventOrderConflictError,
        RepositoryIdentityConflictError,
    ) as error:
        raise GitHubDeliveryPollerError(
            "GitHub delivery push conflicts with local workflow state"
        ) from error
    return "accepted" if result.accepted else "deduplicated"


def _ingest_pull_request(*, delivery_id: str, payload: dict[str, Any], body: bytes) -> str:
    action = payload.get("action")
    if action not in ACCEPTED_ACTIONS:
        return f"ignored:action={action}"
    if _is_github_managed_description_update(payload):
        return "ignored:diffuse_description_update"
    event = normalize_pull_request_event(payload, delivery_id=delivery_id, action=action)
    try:
        result = enqueue_pull_request(event, body)
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="pull_request")
        return "ignored:repository_not_onboarded"
    except (
        DeliveryConflictError,
        EventOrderConflictError,
        RepositoryIdentityConflictError,
    ) as error:
        raise GitHubDeliveryPollerError(
            "GitHub delivery pull request conflicts with local workflow state"
        ) from error
    if result.state == "auto_review_disabled":
        return "ignored:auto_review_disabled"
    return "accepted" if result.accepted else "deduplicated"


def _ingest_issue_comment(*, delivery_id: str, payload: dict[str, Any], body: bytes) -> str:
    if payload.get("action") != "created":
        return f"ignored:action={payload.get('action')}"
    manual_request = normalize_manual_review_request(payload)
    if manual_request is None:
        return "ignored:not_manual_review_trigger"
    try:
        event = asyncio.run(
            fetch_manual_pull_request_event(manual_request, delivery_id=delivery_id)
        )
    except Exception as error:  # noqa: BLE001 — surface GitHub fetch failures as poller errors
        raise GitHubDeliveryPollerError(
            f"Could not load the pull request for a manual @diffuse review: {error}"
        ) from error
    try:
        result = enqueue_pull_request(event, body)
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="pull_request")
        return "ignored:repository_not_onboarded"
    except (
        DeliveryConflictError,
        EventOrderConflictError,
        RepositoryIdentityConflictError,
    ) as error:
        raise GitHubDeliveryPollerError(
            "GitHub delivery manual review conflicts with local workflow state"
        ) from error
    return "accepted" if result.accepted else "deduplicated"


def _ingest_review_comment(*, delivery_id: str, payload: dict[str, Any], body: bytes) -> str:
    if payload.get("action") != "created":
        return f"ignored:action={payload.get('action')}"
    feedback = normalize_review_feedback_comment_event(payload, delivery_id=delivery_id)
    feedback_state: str | None = None
    if feedback is not None:
        try:
            feedback_state = record_review_feedback(feedback, body)
        except RepositoryNotOnboardedError:
            _record_not_onboarded(feedback, event_name="review_feedback")
            return "ignored:repository_not_onboarded"
        except RepositoryIdentityConflictError as error:
            raise GitHubDeliveryPollerError(
                "GitHub delivery review feedback conflicts with local workflow state"
            ) from error
    conversation = normalize_review_conversation_event(payload, delivery_id=delivery_id)
    if conversation is None:
        if feedback_state in {"recorded", "duplicate"}:
            return f"recorded:review_feedback:{feedback_state}"
        return "ignored:not_review_thread_question"
    try:
        result = enqueue_review_conversation(conversation, body)
    except RepositoryNotOnboardedError:
        _record_not_onboarded(conversation, event_name="review_conversation")
        return "ignored:repository_not_onboarded"
    except (DeliveryConflictError, RepositoryIdentityConflictError) as error:
        raise GitHubDeliveryPollerError(
            "GitHub delivery review conversation conflicts with local workflow state"
        ) from error
    if result.state.startswith("ignored:"):
        return result.state
    return "accepted" if result.accepted else "deduplicated"


def ingest_event(*, delivery_id: str, event_name: str, payload: dict[str, Any]) -> str:
    """Idempotently admit a trusted GitHub delivery into local workflow state.

    Keep this in parity with the direct ``/webhook/github`` path so the GitHub
    Integration Service poller admits the same review, feedback, and manual
    trigger events an operator would get from a standalone App webhook.
    """
    body = _payload_bytes(payload)
    if event_name == "ping":
        return "ignored:ping"
    if event_name == "installation":
        # Hosted ingress deactivates/revokes on uninstall. Locally there is
        # nothing to enqueue; acknowledge so the delivery does not retry forever.
        return f"ignored:installation:{payload.get('action')}"
    if event_name == "push":
        return _ingest_push(delivery_id=delivery_id, payload=payload, body=body)
    if event_name == "pull_request":
        return _ingest_pull_request(delivery_id=delivery_id, payload=payload, body=body)
    if event_name == "issue_comment":
        return _ingest_issue_comment(delivery_id=delivery_id, payload=payload, body=body)
    if event_name == "pull_request_review_comment":
        return _ingest_review_comment(delivery_id=delivery_id, payload=payload, body=body)
    return "ignored:unsupported_event"


def pull_once(config: DeliveryPollerConfiguration) -> int:
    try:
        response = httpx.post(
            f"{config.url}/v1/events/pull",
            headers=_headers(config),
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise GitHubDeliveryPollerError(
            f"Could not reach the GitHub Integration Service: {error}"
        ) from error
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise GitHubDeliveryPollerError(
            "GitHub Integration Service rejected this instance credential"
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise GitHubDeliveryPollerError(
            f"GitHub delivery pull failed with HTTP {response.status_code}"
        )
    try:
        events = response.json()["events"]
    except (ValueError, KeyError, TypeError) as error:
        raise GitHubDeliveryPollerError(
            "GitHub delivery pull returned an invalid response"
        ) from error
    if not isinstance(events, list):
        raise GitHubDeliveryPollerError("GitHub delivery pull returned invalid events")
    for envelope in events:
        try:
            delivery_id = str(envelope["delivery_id"])
            event_name = str(envelope["event"])
            payload = envelope["payload"]
            signature = str(envelope["signature"])
        except (KeyError, TypeError) as error:
            raise GitHubDeliveryPollerError(
                "GitHub delivery pull returned an invalid delivery envelope"
            ) from error
        if not isinstance(payload, dict) or not delivery_id or not event_name:
            raise GitHubDeliveryPollerError(
                "GitHub delivery pull returned an invalid delivery envelope"
            )
        _verify_event(
            config,
            delivery_id=delivery_id,
            event_name=event_name,
            payload=payload,
            signature=signature,
        )
        outcome = ingest_event(
            delivery_id=delivery_id,
            event_name=event_name,
            payload=payload,
        )
        try:
            acknowledgement = httpx.post(
                f"{config.url}/v1/events/{delivery_id}/ack",
                headers=_headers(config),
                timeout=scm_api_timeout_seconds(),
            )
        except httpx.HTTPError as error:
            raise GitHubDeliveryPollerError(
                f"Could not acknowledge GitHub delivery {delivery_id}: {error}"
            ) from error
        if acknowledgement.status_code >= httpx.codes.BAD_REQUEST:
            raise GitHubDeliveryPollerError(
                f"GitHub delivery acknowledgement was rejected for {delivery_id}"
            )
        LOGGER.info("Consumed GitHub delivery %s: %s", delivery_id, outcome)
    return len(events)


def run_forever(config: DeliveryPollerConfiguration) -> None:
    while True:
        try:
            received = pull_once(config)
        except GitHubDeliveryPollerError:
            LOGGER.exception("GitHub delivery poll failed")
            received = 0
        if received == 0:
            time.sleep(config.poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll GitHub deliveries into self-hosted Diffuse"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = configuration()
    if config is None:
        parser.error(
            f"{GITHUB_INTEGRATION_URL_VARIABLE}, {GITHUB_INTEGRATION_TOKEN_VARIABLE}, and "
            f"{GITHUB_DELIVERY_SIGNING_KEY_VARIABLE} "
            "must be configured"
        )
    if args.once:
        pull_once(config)
    else:
        run_forever(config)


if __name__ == "__main__":  # pragma: no cover - command entrypoint
    main()
