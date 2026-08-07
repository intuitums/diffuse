"""Gate A session capability and structured-result contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from service.agents.capability import (
    CAPABILITY_OPERATIONS,
    CapabilityError,
    CapabilityRequestBudget,
    mint_session_capability,
    verify_session_capability,
)
from service.agents.result import (
    AGENT_ERROR_CODES,
    AgentSessionResult,
    failure_result,
    success_result,
)

SECRET = b"unit-test-capability-secret"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _mint(**overrides):
    values = {
        "secret": SECRET,
        "runtime": "claude",
        "profile": "review",
        "repository_id": 7,
        "snapshot_id": 11,
        "head_sha": "abc1234",
        "operations": ("search_code", "read_diff", "read_policy"),
        "pull_request_number": 42,
        "now": NOW,
        "capability_id": "cap_test",
    }
    values.update(overrides)
    return mint_session_capability(**values)


def test_mint_and_verify_round_trip():
    minted = _mint()
    claims = verify_session_capability(
        minted.token,
        secret=SECRET,
        now=NOW,
        required_operation="search_code",
        repository_id=7,
        snapshot_id=11,
    )
    assert claims == minted.capability
    assert claims.allows("read_policy")
    assert not claims.allows("read_file")
    assert "capability_id" in claims.as_audit_dict()


def test_expired_capability_is_refused():
    minted = _mint(ttl_seconds=30)
    with pytest.raises(CapabilityError, match="expired"):
        verify_session_capability(
            minted.token,
            secret=SECRET,
            now=NOW + timedelta(seconds=31),
        )


def test_tampered_signature_is_refused():
    minted = _mint()
    token, signature = minted.token.rsplit(".", 1)
    with pytest.raises(CapabilityError, match="signature"):
        verify_session_capability(f"{token}.00{signature[2:]}", secret=SECRET, now=NOW)


def test_operation_outside_allowlist_is_refused():
    minted = _mint(operations=("search_code",))
    with pytest.raises(CapabilityError, match="does not allow operation"):
        verify_session_capability(
            minted.token,
            secret=SECRET,
            now=NOW,
            required_operation="read_diff",
        )


def test_unknown_operation_cannot_be_minted():
    with pytest.raises(CapabilityError, match="unknown values"):
        _mint(operations=("search_code", "shell_exec"))


def test_request_budget_exhausts():
    minted = _mint(max_requests=2)
    budget = CapabilityRequestBudget(minted.capability)
    budget.consume()
    budget.consume()
    with pytest.raises(CapabilityError, match="budget is exhausted"):
        budget.consume()


def test_success_and_failure_results_are_mutually_exclusive():
    ok = success_result(
        stage="structured_output",
        audit_ref="audit-1",
        runtime="claude",
        profile="review",
        capability_id="cap_test",
        value={"summary": "ok"},
        turns_used=3,
        wall_time_seconds=1.5,
    )
    assert ok.ok is True
    assert ok.value == {"summary": "ok"}

    failed = failure_result(
        stage="structured_output",
        audit_ref="audit-2",
        runtime="codex",
        profile="review",
        capability_id="cap_test",
        error_code="structured_output_invalid",
        error_message="schema mismatch",
    )
    assert failed.ok is False
    assert failed.error_code in AGENT_ERROR_CODES

    with pytest.raises(ValidationError):
        AgentSessionResult(
            ok=True,
            stage="structured_output",
            audit_ref="audit-3",
            runtime="claude",
            profile="review",
            capability_id="cap_test",
            value={"summary": "ok"},
            error_code="internal_error",
            error_message="should not be present",
        )


def test_capability_operations_cover_the_gate_b_surface():
    """The allowlist is the product surface; keep it intentional."""

    assert "search_code" in CAPABILITY_OPERATIONS
    assert "read_policy" in CAPABILITY_OPERATIONS
    assert "shell_exec" not in CAPABILITY_OPERATIONS
    assert "github_write" not in CAPABILITY_OPERATIONS
