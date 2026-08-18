from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from diffuse.repository.policy.models import PolicyLayer, RepositoryConfig, RepositoryPolicySnapshot
from diffuse.repository.policy.resolve import resolve_review_policy
from diffuse.review.agent_client import (
    NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
    AgentRuntime,
    NativeRunnerError,
    NativeSessionDispatch,
    _accept_completion,
    _report_from_runner,
    validate_agent_clients,
)
from diffuse.review.report_assembly import fingerprint
from diffuse.review.request import ReviewRequest
from diffuse_protocol import AgentInvestigationRole, opposite_agent_runtime
from diffuse_protocol.artifact import SourceArtifact
from diffuse_protocol.result import canonical_result_payload_digest
from diffuse_protocol.review import CandidateFinding

DIFF = """\\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = False
+new = True
"""


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _result():
    return {
        "schema_version": 1,
        "runtime": "codex",
        "session_id": "session-1",
        "capability_id": "capability-1",
        "summary": "One issue found.",
        "risk_score": 4,
        "audit_reference": "session-1",
        "findings": [
            {
                "title": "Missing check",
                "body": "The new branch dereferences an optional result.",
                "severity": "high",
                "category": "correctness",
                "confidence": 0.9,
                "file_path": "app.py",
                "line": 1,
                "side": "RIGHT",
                "evidence": "value.method()",
            }
        ],
        "prompt_tokens": 12,
        "completion_tokens": 8,
    }


def _verification_result(candidate_result_digest: str, **overrides):
    payload = {
        "schema_version": 1,
        "runtime": "claude",
        "session_id": "session-2",
        "capability_id": "capability-2",
        "summary": "Verified one issue.",
        "risk_score": 4,
        "audit_reference": "session-2",
        "candidate_result_digest": candidate_result_digest,
        "decisions": [
            {
                "candidate_id": "candidate-0",
                "keep": True,
                "confidence": 0.91,
                "rationale": "Confirmed in the workspace.",
            }
        ],
        "prompt_tokens": 5,
        "completion_tokens": 3,
    }
    payload.update(overrides)
    return payload


def test_native_runtime_dispatches_the_verifier_to_the_opposite_runner(monkeypatch):
    candidate_result = _result()
    verifier_result = _verification_result(canonical_result_payload_digest(candidate_result))
    seen = {"dispatches": [], "posts": []}

    def post(url, *, json, timeout):
        seen["posts"].append((url, json, timeout))
        if "codex" in url:
            return _Response(
                {
                    "runtime": "codex",
                    "session_id": "session-1",
                    "capability_id": "capability-1",
                    "runner_id": "host-codex-1",
                    "status": "accepted",
                },
                202,
            )
        return _Response(
            {
                "runtime": "claude",
                "session_id": "session-2",
                "capability_id": "capability-2",
                "runner_id": "host-claude-1",
                "status": "accepted",
            },
            202,
        )

    def get(url, *, timeout, headers):
        assert timeout == NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS
        if url.endswith("session-1"):
            assert headers == {"X-Diffuse-Investigation-Capability": "capability-token"}
            return _Response(
                {
                    "runtime": "codex",
                    "session_id": "session-1",
                    "capability_id": "capability-1",
                    "runner_id": "host-codex-1",
                    "status": "completed",
                    "result": candidate_result,
                }
            )
        assert headers == {"X-Diffuse-Investigation-Capability": "verifier-capability-token"}
        return _Response(
            {
                "runtime": "claude",
                "session_id": "session-2",
                "capability_id": "capability-2",
                "runner_id": "host-claude-1",
                "status": "completed",
                "result": verifier_result,
            }
        )

    def sign(envelope):
        seen["dispatches"].append(envelope)
        return f"signed-{len(seen['dispatches'])}"

    verifier_template = _session(
        session_id="session-2",
        runtime="claude",
        capability="verifier-capability-token",
        capability_id="capability-2",
        role=AgentInvestigationRole.VERIFIER,
        turn_budget=12,
        timeout_seconds=300,
    )
    captured_factory = {}

    def native_verifier_factory(runtime, payload, digest):
        captured_factory.update(runtime=runtime, payload=payload, digest=digest)
        return _session(
            session_id=verifier_template.session_id,
            runtime=verifier_template.runtime,
            capability=verifier_template.capability,
            capability_id=verifier_template.capability_id,
            role=verifier_template.role,
            turn_budget=verifier_template.turn_budget,
            timeout_seconds=verifier_template.timeout_seconds,
            input_result=payload,
            input_result_digest=digest,
        )

    monkeypatch.setattr("diffuse.review.agent_client.httpx.post", post)
    monkeypatch.setattr("diffuse.review.agent_client.httpx.get", get)
    monkeypatch.setattr("diffuse.review.agent_client.sign_dispatch", sign)
    monkeypatch.setattr("diffuse.review.agent_client._accept_completion", lambda *_args: None)
    monkeypatch.setattr("diffuse.review.agent_client._record_lifecycle", lambda *_args: None)
    monkeypatch.setattr("diffuse.review.agent_client.time.sleep", lambda *_args: None)

    report = AgentRuntime("codex").generate(
        ReviewRequest(
            diff_text=(
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1 +1 @@\n"
                "+value.method()\n"
            ),
            agent_investigation=_session(),
            native_verifier_factory=native_verifier_factory,
        )
    )

    assert seen["posts"] == [
        (
            "http://agent-host-codex:8010/v1/investigations",
            {"envelope": "signed-1"},
            NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
        ),
        (
            "http://agent-host-claude:8010/v1/investigations",
            {"envelope": "signed-2"},
            NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
        ),
    ]
    candidate_dispatch, verifier_dispatch = seen["dispatches"]
    assert candidate_dispatch.runtime == "codex"
    assert candidate_dispatch.role is AgentInvestigationRole.CANDIDATE
    assert candidate_dispatch.input_result is None
    assert verifier_dispatch.runtime == "claude"
    assert verifier_dispatch.role is AgentInvestigationRole.VERIFIER
    assert verifier_dispatch.input_result == candidate_result
    assert verifier_dispatch.input_result_digest == canonical_result_payload_digest(
        candidate_result
    )
    assert captured_factory == {
        "runtime": opposite_agent_runtime("codex"),
        "payload": candidate_result,
        "digest": canonical_result_payload_digest(candidate_result),
    }
    assert report.findings[0].fingerprint
    assert report.prompt_tokens == 17
    assert report.verifier_prompt_tokens == 5


@pytest.mark.parametrize(
    ("candidate_overrides", "verifier_overrides", "message"),
    [
        ({"role": AgentInvestigationRole.VERIFIER}, {}, "candidate investigation first"),
        ({}, {"runtime": "codex"}, "not independent"),
        ({}, {"role": AgentInvestigationRole.CANDIDATE}, "verifier investigation second"),
        ({}, {"input_result_digest": "0" * 64}, "not bound"),
    ],
)
def test_native_runtime_rejects_misrouted_investigation_roles(
    monkeypatch, candidate_overrides, verifier_overrides, message
):
    candidate_payload = _result()
    digest = canonical_result_payload_digest(candidate_payload)
    candidate = _session(**candidate_overrides)
    verifier_values = {
        "session_id": "session-2",
        "runtime": "claude",
        "capability_id": "capability-2",
        "role": AgentInvestigationRole.VERIFIER,
        "input_result": candidate_payload,
        "input_result_digest": digest,
        **verifier_overrides,
    }
    monkeypatch.setattr(
        "diffuse.review.agent_client._run_investigation",
        lambda *_args: candidate_payload,
    )

    with pytest.raises(ValueError, match=message):
        AgentRuntime("codex").generate(
            ReviewRequest(
                diff_text=DIFF,
                agent_investigation=candidate,
                native_verifier_factory=lambda *_args: _session(**verifier_values),
            )
        )


def test_native_report_keeps_only_verifier_approved_candidates(monkeypatch):
    monkeypatch.setenv("MIN_REVIEW_CONFIDENCE", "0.80")
    valid = {
        "title": "The new branch removes the authorization guard",
        "body": "The changed line replaces the authorization guard.",
        "severity": "high",
        "category": "correctness",
        "confidence": 0.96,
        "file_path": "app.py",
        "line": 1,
        "side": "RIGHT",
        "evidence": "new = True",
    }
    candidate_payload = {
        "summary": "Candidate found one issue.",
        "risk_score": 1,
        "findings": [valid],
        "prompt_tokens": 12,
        "completion_tokens": 8,
    }
    verifier_payload = {
        "summary": "Confirmed the missing guard.",
        "risk_score": 1,
        "decisions": [
            {
                "candidate_id": "candidate-0",
                "keep": True,
                "confidence": 0.90,
                "rationale": "Confirmed in the workspace.",
            }
        ],
        "prompt_tokens": 5,
        "completion_tokens": 3,
    }

    report = _report_from_runner(candidate_payload, verifier_payload, ReviewRequest(diff_text=DIFF))

    assert len(report.findings) == 1
    assert report.findings[0].title == valid["title"]
    assert report.findings[0].fingerprint == fingerprint(
        CandidateFinding.model_validate(valid), valid["title"]
    )
    assert report.risk_score == 7
    assert report.prompt_tokens == 17
    assert report.verifier_prompt_tokens == 5
    assert report.confidence_score == 2


def test_native_report_rejects_omitted_verifier_decisions():
    candidate_payload = {
        "summary": "Candidate found one issue.",
        "risk_score": 7,
        "findings": [
            {
                "title": "Missing check",
                "body": "The new branch dereferences an optional result.",
                "severity": "high",
                "category": "correctness",
                "confidence": 0.9,
                "file_path": "app.py",
                "line": 1,
                "side": "RIGHT",
                "evidence": "value.method()",
            }
        ],
        "prompt_tokens": 12,
        "completion_tokens": 8,
    }
    verifier_payload = {
        "summary": "No issue survived verification.",
        "risk_score": 9,
        "decisions": [],
        "prompt_tokens": 5,
        "completion_tokens": 3,
    }

    report = _report_from_runner(candidate_payload, verifier_payload, ReviewRequest(diff_text=DIFF))

    assert report.findings == []
    assert report.risk_score == 0
    assert report.summary == "No issue survived verification."
    assert report.verifier_completion_tokens == 3


def test_native_report_cannot_publish_a_policy_disabled_diff():
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {"version": 1, "review": {"enabled": False}}
                    ),
                ),
            )
        ),
        {"app.py"},
    )

    report = _report_from_runner(
        {"summary": "Ignored.", "risk_score": 10, "findings": []},
        {"summary": "Ignored.", "risk_score": 0, "decisions": []},
        ReviewRequest(diff_text=DIFF, policy=policy),
    )

    assert report.skip_reason == "all_files_disabled"
    assert not report.publication_enabled
    assert not report.inline_comments_enabled


def test_startup_validates_the_selected_candidate_and_opposite_verifier_runtimes(monkeypatch):
    checked = []

    def get(url, *, timeout):
        checked.append((url, timeout))
        return _Response({"state": "ready"})

    monkeypatch.setenv("REVIEW_AGENT", "codex")
    monkeypatch.setattr("diffuse.review.agent_client.httpx.get", get)

    validate_agent_clients()

    assert checked == [
        ("http://agent-host-codex:8010/v1/status", 5),
        ("http://agent-host-claude:8010/v1/status", 5),
    ]


def _session(**overrides):
    values = dict(
        session_id="session-1",
        runtime="codex",
        repository_id=7,
        pull_request_id=11,
        snapshot_id=13,
        base_sha="a" * 40,
        head_sha="b" * 40,
        capability="capability-token",
        capability_id="capability-1",
        context_plan_fingerprint="1" * 64,
        role=AgentInvestigationRole.CANDIDATE,
        turn_budget=24,
        timeout_seconds=600,
        max_result_bytes=256_000,
        source_artifact=SourceArtifact(b"test source archive", manifest_digest="0" * 64),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        input_result=None,
        input_result_digest=None,
    )
    values.update(overrides)
    return NativeSessionDispatch(**values)


def test_worker_rejects_a_result_bound_to_a_different_investigation():
    payload = {**_result(), "audit_reference": "other-session"}
    with pytest.raises(NativeRunnerError, match="audit_reference_mismatch"):
        _accept_completion(payload, _session())


def test_worker_rejects_malformed_runner_json_before_persisting():
    payload = {**_result(), "unexpected": True}
    with pytest.raises(NativeRunnerError, match="extra_field") as raised:
        _accept_completion(payload, _session())

    assert raised.value.code == "invalid_result"


@pytest.mark.parametrize(
    ("build_payload", "match"),
    [
        (
            lambda digest: {**_verification_result(digest), "candidate_result_digest": "f" * 64},
            "candidate_result_digest_mismatch",
        ),
        (
            lambda digest: _verification_result(
                digest,
                decisions=[
                    {
                        "candidate_id": "candidate-1",
                        "keep": True,
                        "confidence": 0.9,
                        "rationale": "Unknown id.",
                    }
                ],
            ),
            "unknown_decision_id",
        ),
        (
            lambda digest: _verification_result(
                digest,
                decisions=[
                    {
                        "candidate_id": "candidate-0",
                        "keep": True,
                        "confidence": 0.9,
                        "rationale": "Keep it.",
                    },
                    {
                        "candidate_id": "candidate-0",
                        "keep": False,
                        "confidence": 0.1,
                        "rationale": "Repeat id.",
                    },
                ],
            ),
            "duplicate_decision_id",
        ),
    ],
)
def test_worker_rejects_invalid_verifier_completions_before_persisting(build_payload, match):
    candidate_payload = _result()
    digest = canonical_result_payload_digest(candidate_payload)
    session = _session(
        session_id="session-2",
        runtime="claude",
        capability_id="capability-2",
        role=AgentInvestigationRole.VERIFIER,
        input_result=candidate_payload,
        input_result_digest=digest,
    )
    payload = build_payload(digest)
    payload["session_id"] = session.session_id
    payload["capability_id"] = session.capability_id
    payload["audit_reference"] = session.session_id

    with pytest.raises(NativeRunnerError, match=match) as raised:
        _accept_completion(payload, session)

    assert raised.value.code == "invalid_result"


def test_worker_surfaces_host_terminal_codes_without_hiding_auth(monkeypatch):
    monkeypatch.setattr("diffuse.review.agent_client.sign_dispatch", lambda _envelope: "signed")
    monkeypatch.setattr(
        "diffuse.review.agent_client._start_or_reconnect",
        lambda *_args, **_kwargs: {
            "runtime": "codex",
            "session_id": "session-1",
            "capability_id": "capability-1",
            "runner_id": "host-codex-1",
            "status": "failed",
            "error_code": "rate_limited",
        },
    )
    monkeypatch.setattr("diffuse.review.agent_client._record_lifecycle", lambda *_args: None)

    with pytest.raises(NativeRunnerError, match="rate limited") as raised:
        AgentRuntime("codex").generate(
            ReviewRequest(diff_text=DIFF, agent_investigation=_session())
        )

    assert raised.value.code == "rate_limited"
