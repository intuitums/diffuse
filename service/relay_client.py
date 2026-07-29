"""Outbound relay client used by a self-hosted Diffuse worker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import httpx

from service.github import relay_delivery_signature
from service.github_app import GitHubAppError, RelayCredentials, relay_credentials
from service.scm import scm_api_timeout_seconds

LOGGER = logging.getLogger(__name__)
MAX_RELAY_RESPONSE_BYTES = 2_000_000


class RelayClientError(RuntimeError):
    pass


def configured() -> bool:
    return relay_credentials() is not None


def _headers(credentials: RelayCredentials) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Accept": "application/json",
        "User-Agent": "diffuse-node",
    }


def _delivery_payload(response: httpx.Response) -> tuple[int, str, str, bytes]:
    if len(response.content) > MAX_RELAY_RESPONSE_BYTES:
        raise RelayClientError("Diffuse relay returned an implausibly large delivery")
    try:
        payload = response.json()
        delivery_id = payload["id"]
        provider = payload["provider"]
        provider_delivery_id = payload["deliveryId"]
        event_name = payload["event"]
        encoded_body = payload["bodyBase64"]
        expected_digest = payload["bodySha256"]
    except (ValueError, KeyError, TypeError) as error:
        raise RelayClientError("Diffuse relay returned an unreadable delivery") from error
    if (
        isinstance(delivery_id, bool)
        or not isinstance(delivery_id, int)
        or delivery_id <= 0
        or provider != "github"
        or not isinstance(provider_delivery_id, str)
        or not 1 <= len(provider_delivery_id) <= 255
        or not isinstance(event_name, str)
        or not 1 <= len(event_name) <= 128
        or not isinstance(encoded_body, str)
        or not isinstance(expected_digest, str)
    ):
        raise RelayClientError("Diffuse relay returned invalid delivery metadata")
    try:
        body = base64.b64decode(encoded_body, validate=True)
    except ValueError as error:
        raise RelayClientError("Diffuse relay returned invalid delivery encoding") from error
    actual_digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise RelayClientError("Diffuse relay delivery digest does not match its body")
    return delivery_id, provider_delivery_id, event_name, body


async def pull_once() -> bool:
    credentials = relay_credentials()
    if credentials is None:
        return False
    timeout = scm_api_timeout_seconds()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{credentials.base_url}/relay/v1/deliveries/next",
                headers=_headers(credentials),
            )
    except httpx.HTTPError as error:
        raise RelayClientError("Could not reach the Diffuse integration relay") from error
    if response.status_code == httpx.codes.NO_CONTENT:
        return False
    if response.status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        raise GitHubAppError("Diffuse relay rejected this node credential")
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise RelayClientError(
            f"Diffuse relay refused delivery polling (HTTP {response.status_code})"
        )
    delivery_id, provider_delivery_id, event_name, body = _delivery_payload(response)

    # Reuse the exact local webhook path through an in-process ASGI transport.
    # Nothing listens inbound: the worker invokes its own application and signs
    # the envelope with the paired-node secret.
    from service.webhook_server import app

    signature = relay_delivery_signature(
        body,
        event_name=event_name,
        delivery_id=provider_delivery_id,
        secret=credentials.token,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://diffuse-node",
        timeout=timeout,
    ) as local:
        local_response = await local.post(
            "/webhook/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event_name,
                "X-GitHub-Delivery": provider_delivery_id,
                "X-Diffuse-Relay-Signature": signature,
            },
        )
    if local_response.status_code >= httpx.codes.BAD_REQUEST:
        LOGGER.warning(
            "Relay delivery was not acknowledged locally delivery=%s event=%s status=%s",
            provider_delivery_id,
            event_name,
            local_response.status_code,
        )
        return True

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            acknowledged = await client.post(
                f"{credentials.base_url}/relay/v1/deliveries/{delivery_id}/ack",
                headers=_headers(credentials),
            )
    except httpx.HTTPError as error:
        raise RelayClientError("Could not acknowledge the relay delivery") from error
    if acknowledged.status_code >= httpx.codes.BAD_REQUEST:
        raise RelayClientError(
            "Diffuse relay refused a delivery acknowledgement "
            f"(HTTP {acknowledged.status_code})"
        )
    return True
