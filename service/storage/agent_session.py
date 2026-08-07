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
