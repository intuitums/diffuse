"""Authenticated webhook ingress for Diffuse."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from functools import partial

import anyio
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from indexer.store import get_conn
from service.agents.context_api import router as context_service_router
from service.github.api import (
    fetch_manual_pull_request_event,
    normalize_manual_review_request,
    normalize_pull_request_event,
    normalize_push_event,
    normalize_review_conversation_event,
    normalize_review_feedback_comment_event,
    verify_signature,
)
from service.hosted.worker import validate_worker_configuration
from service.hosted.workflow import (
    REPOSITORY_NOT_ONBOARDED_REASON,
    DeliveryConflictError,
    EnqueueResult,
    EventOrderConflictError,
    RepositoryNotOnboardedError,
    enqueue_repository_index_event,
    enqueue_review_conversation_event,
    enqueue_review_event,
    record_webhook_rejection,
)
from service.review.description import is_managed_review_description_change
from service.scm import (
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
)
from service.storage.feedback import record_review_comment_feedback
from service.storage.migrations import verify_database_current

LOGGER = logging.getLogger(__name__)

ACCEPTED_ACTIONS = {
    "closed",
    "edited",
    "labeled",
    "opened",
    "ready_for_review",
    "reopened",
    "synchronize",
    "unlabeled",
}
MAX_WEBHOOK_BODY_BYTES = 1_000_000


async def _read_bounded_webhook_body(request: Request) -> bytes:
    """Reject oversized webhook bodies before buffering the full payload."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook Content-Length is invalid",
            ) from error
        if declared < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook Content-Length is invalid",
            )
        if declared > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body exceeds Diffuse's size limit",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body exceeds Diffuse's size limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_database_schema() -> None:
    with closing(get_conn()) as conn:
        verify_database_current(conn)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The API resolves the same hot-path configuration the worker does -- it
    # answers code questions in-process and enqueues the jobs the worker later
    # runs -- but it used to check only the database schema. Failing here keeps
    # readiness aligned with worker configuration and Agent access.
    # The API deliberately cannot reach credential-isolated runner control
    # networks. The worker validates their availability before claiming work.
    await anyio.to_thread.run_sync(
        partial(validate_worker_configuration, verify_agent_clients=False)
    )
    await anyio.to_thread.run_sync(_verify_database_schema)
    yield


app = FastAPI(
    title="Diffuse",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(context_service_router)

# Keep the original public name for callers upgrading from the first foundation.
verify_github_signature = verify_signature


def _is_github_managed_description_update(payload: dict) -> bool:
    if payload.get("action") != "edited":
        return False
    changes = payload.get("changes")
    pull_request = payload.get("pull_request")
    if (
        not isinstance(changes, dict)
        or set(changes) != {"body"}
        or not isinstance(pull_request, dict)
    ):
        return False
    body_change = changes.get("body")
    if not isinstance(body_change, dict):
        return False
    return is_managed_review_description_change(
        body_change.get("from"),
        pull_request.get("body"),
    )


def _record_not_onboarded(event, *, event_name: str) -> None:
    """Leave a durable trace that Diffuse turned a delivery away.

    The caller's transaction rolled back when RepositoryNotOnboardedError was
    raised, so this needs its own connection. Recording must never turn a
    rejected delivery into a failed request, hence the broad guard: the 409 the
    caller is about to raise is the important part.
    """
    LOGGER.warning(
        "Rejected %s delivery %s for %s: repository is not onboarded",
        event_name,
        event.delivery_id,
        event.repo_full_name,
    )
    try:
        with closing(get_conn()) as conn, conn:
            record_webhook_rejection(
                conn,
                scm_provider=event.provider,
                scm_base_url=event.scm_base_url,
                delivery_id=event.delivery_id,
                event_name=event_name,
                repo_full_name=event.repo_full_name,
                reason=REPOSITORY_NOT_ONBOARDED_REASON,
            )
    except Exception:
        LOGGER.exception("Could not record the rejected webhook delivery")


def enqueue_pull_request(event: PullRequestEvent, body: bytes) -> EnqueueResult:
    payload_sha256 = hashlib.sha256(body).hexdigest()
    try:
        with closing(get_conn()) as conn, conn:
            return enqueue_review_event(
                conn,
                event,
                payload_sha256=payload_sha256,
            )
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="pull_request")
        raise


def enqueue_repository_push(event: PushEvent, body: bytes) -> EnqueueResult:
    payload_sha256 = hashlib.sha256(body).hexdigest()
    try:
        with closing(get_conn()) as conn, conn:
            return enqueue_repository_index_event(
                conn,
                event,
                payload_sha256=payload_sha256,
            )
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="push")
        raise


def enqueue_review_conversation(
    event: ReviewConversationEvent,
    body: bytes,
) -> EnqueueResult:
    payload_sha256 = hashlib.sha256(body).hexdigest()
    try:
        with closing(get_conn()) as conn, conn:
            return enqueue_review_conversation_event(
                conn,
                event,
                payload_sha256=payload_sha256,
            )
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="review_conversation")
        raise


def record_review_feedback(
    event: ReviewFeedbackCommentEvent,
    body: bytes,
) -> str:
    payload_sha256 = hashlib.sha256(body).hexdigest()
    try:
        with closing(get_conn()) as conn, conn:
            return record_review_comment_feedback(
                conn,
                event,
                payload_sha256=payload_sha256,
            )
    except RepositoryNotOnboardedError:
        _record_not_onboarded(event, event_name="review_feedback")
        raise


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    response: Response,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    body = await _read_bounded_webhook_body(request)
    verify_signature(
        body,
        x_hub_signature_256,
        os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
    )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body is not valid JSON",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body must be a JSON object",
        )

    if x_github_event == "ping":
        return {"status": "ok"}
    if x_github_event == "push":
        event = normalize_push_event(payload, delivery_id=x_github_delivery)
        if event is None:
            return {"status": "ignored", "reason": "not a default-branch update"}
        try:
            result = await anyio.to_thread.run_sync(partial(enqueue_repository_push, event, body))
        except RepositoryNotOnboardedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repository must be onboarded before indexing can be queued",
            ) from error
        except (DeliveryConflictError, EventOrderConflictError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook delivery conflicts with previously accepted state",
            ) from error

        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "status": "accepted" if result.accepted else "deduplicated",
            "repo": event.repo_full_name,
            "revision": event.after_sha,
            "delivery": event.delivery_id,
            "job_id": result.job_id,
            "queue_state": result.state,
        }
    if x_github_event == "issue_comment":
        if payload.get("action") != "created":
            return {
                "status": "ignored",
                "reason": f"action={payload.get('action')}",
            }
        manual_request = normalize_manual_review_request(payload)
        if manual_request is None:
            return {
                "status": "ignored",
                "reason": "not an authorized @diffuse pull-request trigger",
            }
        event = await fetch_manual_pull_request_event(
            manual_request,
            delivery_id=x_github_delivery,
        )
        try:
            result = await anyio.to_thread.run_sync(
                partial(enqueue_pull_request, event, body)
            )
        except RepositoryNotOnboardedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repository must be indexed before reviews can be queued",
            ) from error
        except (DeliveryConflictError, EventOrderConflictError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook delivery conflicts with previously accepted state",
            ) from error
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "status": "accepted" if result.accepted else "deduplicated",
            "trigger": "manual",
            "requested_by": manual_request.requested_by,
            "repo": event.repo_full_name,
            "pr": event.number,
            "revision": event.head_sha,
            "delivery": event.delivery_id,
            "job_id": result.job_id,
            "queue_state": result.state,
        }
    if x_github_event == "pull_request_review_comment":
        if payload.get("action") != "created":
            return {
                "status": "ignored",
                "reason": f"action={payload.get('action')}",
            }
        feedback = normalize_review_feedback_comment_event(
            payload,
            delivery_id=x_github_delivery,
        )
        feedback_state = None
        if feedback is not None:
            try:
                feedback_state = await anyio.to_thread.run_sync(
                    partial(record_review_feedback, feedback, body)
                )
            except RepositoryNotOnboardedError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Repository must be indexed before review feedback "
                        "can be recorded"
                    ),
                ) from error
        conversation = normalize_review_conversation_event(
            payload,
            delivery_id=x_github_delivery,
        )
        if conversation is None:
            if feedback_state in {"recorded", "duplicate"}:
                return {
                    "status": "recorded",
                    "trigger": "review_feedback",
                    "repo": feedback.repo_full_name,
                    "pr": feedback.number,
                    "thread_root": feedback.root_comment_id,
                    "feedback_state": feedback_state,
                }
            return {
                "status": "ignored",
                "reason": "not an authorized @diffuse review-thread question",
                "feedback_state": feedback_state,
            }
        try:
            result = await anyio.to_thread.run_sync(
                partial(enqueue_review_conversation, conversation, body)
            )
        except RepositoryNotOnboardedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repository must be indexed before review questions can be queued",
            ) from error
        except DeliveryConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook delivery conflicts with previously accepted state",
            ) from error
        if result.job_id is not None:
            response.status_code = status.HTTP_202_ACCEPTED
        return {
            "status": (
                "accepted"
                if result.accepted
                else "ignored"
                if result.state.startswith("ignored:")
                else "deduplicated"
            ),
            "trigger": "review_conversation",
            "requested_by": conversation.author,
            "repo": conversation.repo_full_name,
            "pr": conversation.number,
            "thread_root": conversation.root_comment_id,
            "revision": conversation.head_sha,
            "delivery": conversation.delivery_id,
            "job_id": result.job_id,
            "queue_state": result.state,
            "feedback_state": feedback_state,
        }
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "unsupported event"}

    action = payload.get("action")
    if action not in ACCEPTED_ACTIONS:
        return {"status": "ignored", "reason": f"action={action}"}
    if _is_github_managed_description_update(payload):
        return {
            "status": "ignored",
            "reason": "diffuse_description_update",
        }

    event = normalize_pull_request_event(
        payload,
        delivery_id=x_github_delivery,
        action=action,
    )
    try:
        result = await anyio.to_thread.run_sync(partial(enqueue_pull_request, event, body))
    except RepositoryNotOnboardedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository must be indexed before reviews can be queued",
        ) from error
    except (DeliveryConflictError, EventOrderConflictError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Webhook delivery conflicts with previously accepted state",
        ) from error

    response.status_code = status.HTTP_202_ACCEPTED
    lifecycle_recorded = result.state in {
        "pull_request_closed",
        "pull_request_merged",
    }
    return {
        "status": (
            "accepted"
            if result.accepted
            else "recorded"
            if lifecycle_recorded
            else "deduplicated"
        ),
        "repo": event.repo_full_name,
        "pr": event.number,
        "revision": event.head_sha,
        "delivery": event.delivery_id,
        "job_id": result.job_id,
        "queue_state": result.state,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness(response: Response):
    try:
        await anyio.to_thread.run_sync(_verify_database_schema)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "database_schema"}
    return {"status": "ready"}
