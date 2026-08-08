"""Capability-authorized, index-backed tools for the isolated runner.

This is intentionally a narrow internal HTTP surface rather than a second
general-purpose API.  The agent-runner has no database or operator token; its
short-lived session capability is sufficient only for the tool and immutable
snapshot named in that capability.
"""

from __future__ import annotations

import time
from contextlib import closing
from functools import partial
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from indexer.store import get_conn
from retriever.context_models import CrossRepositoryContextPlan
from service.agents.capability_auth import session_capability_signing_key
from service.agents.contract import (
    CapabilityError,
    CapabilityExpired,
    CapabilityScopeMismatch,
    SessionCapability,
    verify_session_capability,
)
from service.code_query import CodeQueryTarget, search_codebase
from service.repositories import get_repository
from service.review.tool_log import record_review_tool_call
from service.storage.agent_session import resolve_agent_session_review_attempt

AGENT_API_PREFIX = "/agent/v1"
MAX_AGENT_SEARCH_LIMIT = 20

router = APIRouter(prefix=AGENT_API_PREFIX, include_in_schema=False)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchCodeRequest(_StrictRequest):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    path: Annotated[str | None, Field(min_length=1, max_length=1024)] = None
    limit: int = Field(default=8, ge=1, le=MAX_AGENT_SEARCH_LIMIT)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Agent session capability is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Agent session capability is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _session_capability(
    authorization: Annotated[str | None, Header()] = None,
) -> SessionCapability:
    """Verify a bearer capability without ever logging or returning its value."""

    try:
        return verify_session_capability(
            _bearer_token(authorization),
            signing_key=session_capability_signing_key(),
            require_operation="search_code",
        )
    except CapabilityExpired as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Agent session expired") from error
    except CapabilityScopeMismatch as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent session is not authorized") from error
    except (CapabilityError, ValueError) as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent session") from error


def _target_for_capability(conn, capability: SessionCapability) -> CodeQueryTarget:
    """Resolve exactly the primary immutable snapshot named by a capability."""

    repository = get_repository(conn, capability.scope.repository_id)
    if repository is None or not repository.enabled:
        raise ValueError("The capability repository is unavailable")

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot.commit_sha
            FROM index_snapshots AS snapshot
            WHERE snapshot.id = %s
              AND snapshot.repository_id = %s
              AND snapshot.status IN ('active', 'superseded')
            """,
            (capability.scope.snapshot_id, capability.scope.repository_id),
        )
        snapshot = cursor.fetchone()
        cursor.execute(
            """
            SELECT head_sha
            FROM pull_requests
            WHERE id = %s
              AND repository_id = %s
            """,
            (capability.scope.pull_request_id, capability.scope.repository_id),
        )
        pull_request = cursor.fetchone()

    if not snapshot:
        raise ValueError("The capability snapshot is unavailable")
    if not pull_request or str(pull_request[0]).lower() != capability.scope.head_sha:
        raise ValueError("The capability pull request revision is unavailable")

    plan = CrossRepositoryContextPlan(
        primary_repository_id=repository.id,
        primary_repository_full_name=repository.full_name,
        primary_snapshot_id=capability.scope.snapshot_id,
        primary_commit_sha=str(snapshot[0]),
    )
    return CodeQueryTarget(
        repository_id=repository.id,
        repository_name=repository.full_name,
        remote=repository.scm_provider,
        remote_url=repository.scm_base_url,
        default_branch=repository.default_branch,
        include_related=False,
        context_plan=plan,
    )


def _search_code(capability: SessionCapability, request: SearchCodeRequest) -> dict[str, object]:
    arguments: dict[str, object] = {
        "query": request.query,
        "path": request.path,
        "limit": request.limit,
    }
    started = time.perf_counter()
    with closing(get_conn()) as conn, conn:
        attempt = resolve_agent_session_review_attempt(conn, capability=capability)
        if attempt is None:
            # A signed capability names source scope, but only a dispatched
            # session tied to the review's current attempt may cause an audit
            # row. This prevents an old, still-unexpired session from adding
            # calls to a retried run.
            raise ValueError("The capability session is no longer active")

        try:
            target = _target_for_capability(conn, capability)
            result = search_codebase(
                target,
                query=request.query,
                path_prefix=request.path,
                limit=request.limit,
            )
        except Exception as error:
            # This connection only performs the capability reads and this log
            # write. Recover it after a database/read failure before recording
            # the failed remote tool call, rather than losing evidence because
            # PostgreSQL rejected every command in the aborted transaction.
            conn.rollback()
            record_review_tool_call(
                conn,
                attempt.review_run_id,
                tool_name="search_code",
                arguments=arguments,
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                failure_code="search_failed",
                failure_detail=(str(error).strip() or error.__class__.__name__)[:2000],
                index_snapshot_ids=(capability.scope.snapshot_id,),
                context_plan_fingerprint=None,
                attempt_started_at=attempt.attempt_started_at,
            )
            raise

        record_review_tool_call(
            conn,
            attempt.review_run_id,
            tool_name="search_code",
            arguments=arguments,
            duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            result=result,
            index_snapshot_ids=(capability.scope.snapshot_id,),
            context_plan_fingerprint=target.context_plan.fingerprint,
            attempt_started_at=attempt.attempt_started_at,
        )
        return result


@router.post("/tools/search-code", summary="Search a capability-pinned code snapshot")
async def search_code(
    request: SearchCodeRequest,
    capability: Annotated[SessionCapability, Depends(_session_capability)],
) -> dict[str, object]:
    try:
        return await anyio.to_thread.run_sync(partial(_search_code, capability, request))
    except ValueError as error:
        # A capability is no longer usable if its precise repository/PR/snapshot
        # binding disappeared or advanced. Do not turn that into a broad lookup.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent session is not authorized") from error
