"""Gate A contract: session capability mint/scope/expiry and result taxonomy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from service.agents.contract import (
    AGENT_RUNTIME_CLAUDE,
    AGENT_RUNTIME_CODEX,
    CAPABILITY_OPERATIONS,
    RESULT_SCHEMA_VERSION,
    AgentRuntimeConfig,
    CapabilityExpired,
    CapabilityMalformed,
    CapabilityScopeMismatch,
    ResultValidationError,
    ResultValidationFailureCode,
    SessionScope,
    mint_session_capability,
    parse_agent_runtime_name,
    validate_agent_session_result,
    verify_session_capability,
)

SIGNING_KEY = b"unit-test-signing-key"


def _scope(**overrides):
    values = {
        "repository_id": 7,
        "pull_request_id": 19,
        "snapshot_id": 3,
        "head_sha": "abc1234",
        "operations": frozenset({"search_code", "get_diff"}),
    }
    values.update(overrides)
    return SessionScope(**values)


def _valid_result(**overrides) -> dict:
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "runtime": AGENT_RUNTIME_CLAUDE,
        "summary": "No blocking issues found.",
        "risk_score": 1.5,
        "audit_reference": "session-record-1",
        "findings": [
            {
                "title": "Nil check missing",
                "body": "The handler dereferences an optional value.",
                "severity": "high",
                "category": "correctness",
                "confidence": 0.9,
                "file_path": "app.py",
                "line": 12,
                "evidence": "value.method()",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_agent_runtime_config_accepts_explicit_runtimes():
    for name in (AGENT_RUNTIME_CLAUDE, AGENT_RUNTIME_CODEX):
        config = AgentRuntimeConfig(runtime=name, turn_budget=8, timeout_seconds=120)
        assert config.runtime == name
        assert parse_agent_runtime_name(name.upper()) == name


def test_agent_runtime_config_refuses_litellm_and_defaults():
    with pytest.raises(ValueError, match="runtime must be one of"):
        AgentRuntimeConfig(runtime="litellm")
    with pytest.raises(ValueError, match="turn_budget must be positive"):
        AgentRuntimeConfig(runtime=AGENT_RUNTIME_CLAUDE, turn_budget=0)


def test_mint_session_capability_round_trips():
    issued = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    grant = mint_session_capability(
        signing_key=SIGNING_KEY,
        runtime=AGENT_RUNTIME_CODEX,
        scope=_scope(),
        ttl=timedelta(minutes=10),
        now=issued,
        capability_id="cap-test-1",
    )
    assert grant.token.startswith("diffuse-cap.")
    verified = verify_session_capability(
        grant.token,
        signing_key=SIGNING_KEY,
        now=issued + timedelta(minutes=1),
    )
    assert verified.capability_id == "cap-test-1"
    assert verified.runtime == AGENT_RUNTIME_CODEX
    assert verified.scope.repository_id == 7
    assert verified.scope.operations == frozenset({"search_code", "get_diff"})
    assert verified.expires_at == issued + timedelta(minutes=10)


def test_capability_scope_refuses_unknown_and_empty_operations():
    with pytest.raises(ValueError, match="unsupported values"):
        _scope(operations=frozenset({"search_code", "write_github"}))
    with pytest.raises(ValueError, match="at least one"):
        _scope(operations=frozenset())
    assert "search_code" in CAPABILITY_OPERATIONS


def test_verify_enforces_operation_and_pin_scope():
    grant = mint_session_capability(
        signing_key=SIGNING_KEY,
        runtime=AGENT_RUNTIME_CLAUDE,
        scope=_scope(),
        now=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    verify_session_capability(
        grant.token,
        signing_key=SIGNING_KEY,
        now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
        require_operation="search_code",
        require_repository_id=7,
        require_snapshot_id=3,
        require_head_sha="abc1234",
        require_runtime=AGENT_RUNTIME_CLAUDE,
    )
    with pytest.raises(CapabilityScopeMismatch, match="does not authorize"):
        verify_session_capability(
            grant.token,
            signing_key=SIGNING_KEY,
            now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
            require_operation="get_file",
        )
    with pytest.raises(CapabilityScopeMismatch, match="repository_id"):
        verify_session_capability(
            grant.token,
            signing_key=SIGNING_KEY,
            now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
            require_repository_id=99,
        )


def test_verify_refuses_expired_and_tampered_tokens():
    issued = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    grant = mint_session_capability(
        signing_key=SIGNING_KEY,
        runtime=AGENT_RUNTIME_CLAUDE,
        scope=_scope(),
        ttl=timedelta(minutes=5),
        now=issued,
    )
    with pytest.raises(CapabilityExpired):
        verify_session_capability(
            grant.token,
            signing_key=SIGNING_KEY,
            now=issued + timedelta(minutes=5),
        )
    with pytest.raises(CapabilityMalformed, match="signature"):
        verify_session_capability(
            grant.token[:-1] + ("0" if grant.token[-1] != "0" else "1"),
            signing_key=SIGNING_KEY,
            now=issued + timedelta(minutes=1),
        )
    with pytest.raises(CapabilityMalformed, match="malformed"):
        verify_session_capability(
            "not-a-capability",
            signing_key=SIGNING_KEY,
            now=issued + timedelta(minutes=1),
        )


def test_validate_agent_session_result_accepts_valid_payload():
    result = validate_agent_session_result(_valid_result())
    assert result.runtime == AGENT_RUNTIME_CLAUDE
    assert result.findings[0].file_path == "app.py"
    assert result.schema_version == RESULT_SCHEMA_VERSION


def test_result_validation_failure_taxonomy_invalid_json():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result("{")
    assert raised.value.code is ResultValidationFailureCode.INVALID_JSON


def test_result_validation_failure_taxonomy_not_an_object():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result([])
    assert raised.value.code is ResultValidationFailureCode.NOT_AN_OBJECT


def test_result_validation_failure_taxonomy_unsupported_version():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result(_valid_result(schema_version=99))
    assert raised.value.code is ResultValidationFailureCode.UNSUPPORTED_VERSION
    assert raised.value.field == "schema_version"


def test_result_validation_failure_taxonomy_missing_field():
    payload = {key: value for key, value in _valid_result().items() if key != "summary"}
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result(payload)
    assert raised.value.code is ResultValidationFailureCode.MISSING_FIELD
    assert raised.value.field == "summary"


def test_result_validation_failure_taxonomy_extra_field():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result({**_valid_result(), "unexpected": True})
    assert raised.value.code is ResultValidationFailureCode.EXTRA_FIELD


def test_result_validation_failure_taxonomy_type_error():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result({**_valid_result(), "risk_score": "high"})
    assert raised.value.code is ResultValidationFailureCode.TYPE_ERROR


def test_result_validation_failure_taxonomy_constraint_violation():
    with pytest.raises(ResultValidationError) as raised:
        validate_agent_session_result({**_valid_result(), "risk_score": 99})
    assert raised.value.code is ResultValidationFailureCode.CONSTRAINT_VIOLATION


def test_result_validation_accepts_json_text():
    result = validate_agent_session_result(json.dumps(_valid_result()))
    assert len(result.findings) == 1
