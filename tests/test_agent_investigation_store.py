"""Durable review-attempt binding for native capability tools."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from service.agents.contract import AgentInvestigationRole, SessionScope, mint_session_capability
from service.storage.agent_investigation import (
    abandon_agent_investigation,
    create_agent_investigation,
    record_agent_investigation_lifecycle,
    request_agent_investigation_cancellation,
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


def test_creating_an_investigation_persists_the_execution_spec():
    capability = _capability()
    connection = _Connection(None)

    handle = create_agent_investigation(
        connection,
        review_job_id=41,
        capability=capability,
        capability_token="capability-token",
        base_sha="b" * 40,
        context_plan_fingerprint="1" * 64,
        role=AgentInvestigationRole.CANDIDATE,
        turn_budget=24,
        timeout_seconds=600,
        max_result_bytes=256_000,
        source_archive_digest="2" * 64,
        source_manifest_digest="3" * 64,
    )

    assert handle.runtime == "codex"
    statement, parameters = connection.cursor_instance.executed
    assert parameters[1:] == (
        41,
        "codex",
        7,
        11,
        13,
        "b" * 40,
        "a" * 40,
        "capability-1",
        parameters[9],
        "1" * 64,
        "candidate",
        24,
        600,
        256_000,
        "2" * 64,
        "3" * 64,
        None,
        capability.expires_at,
    )
    assert "base_sha" in statement
    assert "context_plan_fingerprint" in statement
    assert "source_archive_digest" in statement
    assert "input_result_digest" in statement
    assert len(parameters[9]) == 64


def test_verifier_investigations_require_the_candidate_result_digest():
    capability = _capability()
    connection = _Connection(None)

    create_agent_investigation(
        connection,
        review_job_id=41,
        capability=capability,
        capability_token="capability-token",
        base_sha="b" * 40,
        context_plan_fingerprint="1" * 64,
        role=AgentInvestigationRole.VERIFIER,
        turn_budget=12,
        timeout_seconds=300,
        max_result_bytes=256_000,
        source_archive_digest="2" * 64,
        source_manifest_digest="3" * 64,
        input_result_digest="4" * 64,
    )

    _statement, parameters = connection.cursor_instance.executed
    assert parameters[-2] == "4" * 64


@pytest.mark.parametrize(
    ("role", "input_result_digest", "match"),
    [
        (AgentInvestigationRole.CANDIDATE, "4" * 64, "forbid input_result_digest"),
        (AgentInvestigationRole.VERIFIER, None, "require input_result_digest"),
    ],
)
def test_investigation_execution_spec_enforces_role_bound_input_digest(
    role, input_result_digest, match
):
    with pytest.raises(ValueError, match=match):
        create_agent_investigation(
            _Connection(None),
            review_job_id=41,
            capability=_capability(),
            capability_token="capability-token",
            base_sha="b" * 40,
            context_plan_fingerprint="1" * 64,
            role=role,
            turn_budget=24,
            timeout_seconds=600,
            max_result_bytes=256_000,
            source_archive_digest="2" * 64,
            source_manifest_digest="3" * 64,
            input_result_digest=input_result_digest,
        )


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
        capability.token_hash,
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
    assert "session.status IN ('dispatched', 'accepted', 'running')" in statement
    assert "session.expires_at > now()" in statement
    assert "review.status = 'generating'" in statement
    assert "session.created_at >= review.started_at" in statement


def test_capability_tool_attempt_rejects_missing_or_stale_session():
    assert resolve_agent_investigation_review_attempt(
        _Connection(None),
        capability=_capability(),
    ) is None


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("failed", "timeout"), ("cancelled", "cancelled")],
)
def test_abandoning_a_session_records_the_terminal_error_code(status, error_code):
    connection = _Connection(None, rowcount=1)

    assert abandon_agent_investigation(
        connection,
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability_id="capability-1",
        status=status,
        error_code=error_code,
    )

    statement, parameters = connection.cursor_instance.executed
    assert parameters == (
        status,
        error_code,
        "a32b1c5d-3c15-4462-a9fe-f191775b3459",
        "codex",
        "capability-1",
    )
    assert "error_code = %s" in statement
    assert "completed_at = COALESCE(completed_at, now())" in statement
    assert "AND status IN ('dispatched', 'accepted', 'running')" in statement


def test_abandoning_a_session_rejects_nonterminal_state():
    with pytest.raises(ValueError, match="failed or cancelled"):
        abandon_agent_investigation(
            _Connection(None),
            session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
            runtime="codex",
            capability_id="capability-1",
            status="completed",
        )


def test_runner_lifecycle_binds_the_first_host_and_only_allows_monotonic_states():
    connection = _Connection(None, rowcount=1)

    assert record_agent_investigation_lifecycle(
        connection,
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability_id="capability-1",
        runner_id="codex-host-a",
        status="accepted",
    )

    statement, parameters = connection.cursor_instance.executed
    assert parameters[-2:] == ("codex-host-a", ["dispatched"])
    assert "runner_id = COALESCE(runner_id, %s)" in statement
    assert "runner_id IS NULL OR runner_id = %s" in statement
    assert "error_code = NULL" in statement


def test_runner_lifecycle_rejects_a_nonrunner_status():
    with pytest.raises(ValueError, match="accepted or running"):
        record_agent_investigation_lifecycle(
            _Connection(None),
            session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
            runtime="codex",
            capability_id="capability-1",
            runner_id="codex-host-a",
            status="completed",
        )


def test_cancellation_is_recorded_only_while_an_investigation_is_active():
    connection = _Connection(None, rowcount=1)

    assert request_agent_investigation_cancellation(
        connection,
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability_id="capability-1",
    )

    statement, parameters = connection.cursor_instance.executed
    assert parameters == ("a32b1c5d-3c15-4462-a9fe-f191775b3459", "codex", "capability-1")
    assert "cancel_requested_at = COALESCE(cancel_requested_at, now())" in statement
    assert "status IN ('dispatched', 'accepted', 'running')" in statement


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
