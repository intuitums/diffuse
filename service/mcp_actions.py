"""Audited durable review-trigger write actions."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import psycopg2.extras

from service.hosted.workflow import enqueue_review_event
from service.scm import PullRequestEvent


def enqueue_review_trigger(
    conn,
    *,
    event: PullRequestEvent,
    repository_id: int,
    authorized_repository_ids: frozenset[int] | None,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
) -> dict[str, object]:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise ValueError("Repository does not exist or is not authorized")
    if actor_kind == "service_token":
        if actor_token_id is None or actor_token_id <= 0:
            raise ValueError("Review trigger actor is invalid")
    elif actor_kind != "operator" or actor_token_id is not None:
        raise ValueError("Review trigger actor is invalid")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id
            FROM repositories
            WHERE id = %s
              AND scm_provider = %s
              AND scm_base_url = %s
              AND full_name = %s
              AND enabled = TRUE
            FOR SHARE
            """,
            (
                repository_id,
                event.provider,
                event.scm_base_url,
                event.repo_full_name,
            ),
        )
        if cursor.fetchone() is None:
            raise ValueError("Repository does not exist or is not authorized")

    serialized = json.dumps(
        event.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    result = enqueue_review_event(
        conn,
        event,
        payload_sha256=hashlib.sha256(serialized).hexdigest(),
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id
            FROM pull_requests
            WHERE repository_id = %s
              AND number = %s
            """,
            (repository_id, event.number),
        )
        pull_request_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO audit_events (
                actor_kind,
                actor_label,
                action,
                resource_kind,
                resource_id,
                repository_id,
                details
            )
            VALUES (
                %s,
                %s,
                'code_review.triggered',
                'pull_request',
                %s,
                %s,
                %s
            )
            """,
            (
                actor_kind,
                actor_label,
                str(pull_request_id),
                repository_id,
                psycopg2.extras.Json(
                    {
                        "pull_request_number": event.number,
                        "head_sha": event.head_sha,
                        "job_id": result.job_id,
                        "queue_state": result.state,
                    }
                ),
            ),
        )
    return {
        "success": result.job_id is not None,
        "message": (
            "Code review triggered successfully"
            if result.job_id is not None
            else "Code review trigger was recorded without a new job"
        ),
        "repository": {
            "id": repository_id,
            "name": event.repo_full_name,
            "remote": event.provider,
            "remoteUrl": event.scm_base_url,
        },
        "prNumber": event.number,
        "headSha": event.head_sha,
        "jobId": result.job_id,
        "queueState": result.state,
    }


# Backwards-compatible internal name for the original MCP-only action.
enqueue_mcp_review_trigger = enqueue_review_trigger
