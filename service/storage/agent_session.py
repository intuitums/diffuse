"""Durable, replay-safe lifecycle for native agent sessions."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from service.agents.contract.capability import SessionCapability


class AgentSessionReplayError(ValueError):
    """A completion does not match the dispatched session."""


@dataclass(frozen=True)
class AgentSessionHandle:
    session_id: str
    runtime: str
    capability_id: str
    expires_at: datetime


@dataclass(frozen=True)
class AgentSessionReviewAttempt:
    """The one active review attempt a capability-backed tool call may audit.

    A native session is created after its review run has entered ``generating``.
    The attempt timestamp is retained here so a tool call that races a retry is
    filed under the attempt that dispatched the session rather than whatever
    attempt happens to be current when the query finishes.
    """

    review_run_id: int
    attempt_started_at: datetime


_ABANDONED_SESSION_STATUSES = frozenset({"failed", "cancelled"})


def create_agent_session(
    conn,
    *,
    review_job_id: int,
    capability: SessionCapability,
    capability_token: str,
) -> AgentSessionHandle:
    session_id = str(uuid.uuid4())
    token_hash = hashlib.sha256(capability_token.encode()).hexdigest()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO agent_sessions (
                id, review_job_id, runtime, repository_id, pull_request_id,
                snapshot_id, head_sha, capability_id, capability_hash, expires_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'dispatched')
            """,
            (
                session_id,
                review_job_id,
                capability.runtime,
                capability.scope.repository_id,
                capability.scope.pull_request_id,
                capability.scope.snapshot_id,
                capability.scope.head_sha,
                capability.capability_id,
                token_hash,
                capability.expires_at,
            ),
        )
    return AgentSessionHandle(
        session_id=session_id,
        runtime=capability.runtime,
        capability_id=capability.capability_id,
        expires_at=capability.expires_at,
    )


def resolve_agent_session_review_attempt(
    conn,
    *,
    capability: SessionCapability,
) -> AgentSessionReviewAttempt | None:
    """Return the active, scope-identical review attempt for a capability.

    The signed capability determines what source may be read. This lookup adds
    the durable lifecycle binding needed to attribute that read to a review
    run. Every scope value that the session persisted at dispatch must still
    equal the verified capability; capability verification already enforces its
    expiry. The session must also have been created during the review attempt
    that is generating now. A reset refreshes
    ``review_runs.started_at``, so sessions from an earlier attempt stop
    matching before they can write into the new attempt's investigation log.
    """

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT review.id, review.started_at
            FROM agent_sessions AS session
            JOIN review_runs AS review
              ON review.workflow_job_id = session.review_job_id
             AND review.repository_id = session.repository_id
             AND review.pull_request_id = session.pull_request_id
             AND review.index_snapshot_id = session.snapshot_id
             AND review.head_sha = session.head_sha
            WHERE session.capability_id = %s
              AND session.runtime = %s
              AND session.repository_id = %s
              AND session.pull_request_id = %s
              AND session.snapshot_id = %s
              AND session.head_sha = %s
              AND session.status = 'dispatched'
              AND session.expires_at > now()
              AND review.status = 'generating'
              AND session.created_at >= review.started_at
            """,
            (
                capability.capability_id,
                capability.runtime,
                capability.scope.repository_id,
                capability.scope.pull_request_id,
                capability.scope.snapshot_id,
                capability.scope.head_sha,
            ),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return AgentSessionReviewAttempt(
        review_run_id=int(row[0]),
        attempt_started_at=row[1],
    )


def abandon_agent_session(
    conn,
    *,
    session_id: str,
    runtime: str,
    capability_id: str,
    status: str,
) -> bool:
    """Retire an unfinished dispatch after its review attempt stops.

    Tool authorization already requires the associated review to be generating,
    but recording the terminal session state closes that window immediately and
    keeps the durable audit trail honest after a runner timeout, failure, or
    supersession. A completion that won the race remains immutable.
    """

    if status not in _ABANDONED_SESSION_STATUSES:
        raise ValueError("agent session status must be failed or cancelled")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE agent_sessions
            SET status = %s
            WHERE id = %s::uuid
              AND runtime = %s
              AND capability_id = %s
              AND status = 'dispatched'
            """,
            (status, session_id, runtime, capability_id),
        )
        return cursor.rowcount == 1


def accept_agent_session_completion(
    conn,
    *,
    session_id: str,
    runtime: str,
    capability_id: str,
    result: bytes,
) -> bool:
    """Accept one expected completion; permit only identical retransmission."""

    digest = hashlib.sha256(result).hexdigest()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE agent_sessions
            SET status = 'completed', result_digest = %s, completed_at = now()
            WHERE id = %s::uuid
              AND runtime = %s
              AND capability_id = %s
              AND status = 'dispatched'
              AND expires_at > now()
            """,
            (digest, session_id, runtime, capability_id),
        )
        if cursor.rowcount == 1:
            return True
        cursor.execute(
            """
            SELECT runtime, capability_id, status, result_digest
            FROM agent_sessions WHERE id = %s::uuid
            """,
            (session_id,),
        )
        existing = cursor.fetchone()
    if not existing:
        raise AgentSessionReplayError("agent session is unknown")
    if tuple(existing[:3]) == (runtime, capability_id, "completed") and existing[3] == digest:
        return False
    raise AgentSessionReplayError("agent session completion does not match dispatch")
