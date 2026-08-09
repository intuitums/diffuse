"""Durable review-attempt binding for native capability tools."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from service.agents.contract import SessionScope, mint_session_capability
from service.storage.agent_investigation import (
    abandon_agent_investigation,
    resolve_agent_investigation_review_attempt,
)


def _capability():
    return mint_session_capability(
        signing_key="s" * 48,
        runtime="codex",
        scope=SessionScope(
            repository_id=7,
            pull_request_id=11,
            snapshot_id=13,
            head_sha="a" * 40,
            operations=frozenset({"search_code"}),
        ),
        now=datetime.now(UTC),
        capability_id="capability-1",
    ).capability


def test_capability_tool_attempt_requires_the_current_scope_identical_session():
    capability = _capability()
    attempt_started_at = datetime.now(UTC)
    connection = _Connection((41, attempt_started_at))

    actual = resolve_agent_investigation_review_attempt(connection, capability=capability)

    assert actual is not None
    assert actual.review_run_id == 41
    assert actual.attempt_started_at == attempt_started_at
    statement, parameters = connection.cursor_instance.executed
    assert parameters == (
        "capability-1",
        "codex",
        7,
        11,
        13,
        "a" * 40,
    )
    # A current capability cannot be borrowed by another review or a retried
    # attempt: the join asserts the durable scope and the creation-time guard.
    assert "review.workflow_job_id = session.review_job_id" in statement
    assert "review.index_snapshot_id = session.snapshot_id" in statement
    assert "session.status = 'dispatched'" in statement
    assert "session.expires_at > now()" in statement
    assert "review.status = 'generating'" in statement
    assert "session.created_at >= review.started_at" in statement


def test_capability_tool_attempt_rejects_missing_or_stale_session():
    assert resolve_agent_investigation_review_attempt(
        _Connection(None),
        capability=_capability(),
    ) is None


@pytest.mark.parametrize("status", ("failed", "cancelled"))
def test_abandoning_a_session_cannot_replace_a_completed_result(status):
    connection = _Connection(None, rowcount=1)

    assert abandon_agent_investigation(
        connection,
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability_id="capability-1",
        status=status,
    )

    statement, parameters = connection.cursor_instance.executed
    assert parameters == (
        status,
        "a32b1c5d-3c15-4462-a9fe-f191775b3459",
        "codex",
        "capability-1",
    )
    assert "AND status = 'dispatched'" in statement


def test_abandoning_a_session_rejects_nonterminal_state():
    with pytest.raises(ValueError, match="failed or cancelled"):
        abandon_agent_investigation(
            _Connection(None),
            session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
            runtime="codex",
            capability_id="capability-1",
            status="completed",
        )


class _Connection:
    def __init__(self, row, *, rowcount: int = 0) -> None:
        self.cursor_instance = _Cursor(row, rowcount=rowcount)

    def cursor(self):
        return self.cursor_instance


class _Cursor:
    def __init__(self, row, *, rowcount: int) -> None:
        self._row = row
        self.rowcount = rowcount
        self.executed: tuple[str, tuple[object, ...]] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.executed = (statement, parameters)

    def fetchone(self):
        return self._row
