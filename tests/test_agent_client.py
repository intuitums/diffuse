from __future__ import annotations

from datetime import UTC, datetime, timedelta

from repository_policy.models import PolicyLayer, RepositoryConfig, RepositoryPolicySnapshot
from repository_policy.resolve import resolve_review_policy
from service.models.review import CandidateFinding
from service.review.agent_client import (
    NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
    AgentRuntime,
    NativeSessionDispatch,
    _report_from_runner,
    validate_agent_clients,
)
from service.review.report_assembly import fingerprint
from service.review.request import ReviewRequest
from service.review.workspace import SourceArtifact

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
            "runtime": "codex",
            "session_id": "session-1",
            "capability_id": "capability-1",
            "summary": "One issue found.",
            "risk_score": 4,
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


def test_native_runtime_calls_only_its_matching_isolated_runner(monkeypatch):
    seen = {}

    def post(url, *, json, timeout):
        seen.update(url=url, json=json, timeout=timeout)
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

    def get(url, *, timeout, headers):
        seen["status_url"] = url
        assert headers == {"X-Diffuse-Investigation-Capability": "capability-token"}
        return _Response(
            {
                "runtime": "codex",
                "session_id": "session-1",
                "capability_id": "capability-1",
                "runner_id": "host-codex-1",
                "status": "completed",
                "result": _result(),
            }
        )

    monkeypatch.setattr("service.review.agent_client.httpx.post", post)
    monkeypatch.setattr("service.review.agent_client.httpx.get", get)
    monkeypatch.setattr("service.review.agent_client.sign_dispatch", lambda _envelope: "signed")
    monkeypatch.setattr("service.review.agent_client._accept_completion", lambda *_args: None)
    monkeypatch.setattr("service.review.agent_client._record_lifecycle", lambda *_args: None)
    monkeypatch.setattr("service.review.agent_client.time.sleep", lambda *_args: None)
    report = AgentRuntime("codex").generate(
        ReviewRequest(
            diff_text=(
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1 +1 @@\n"
                "+value.method()\n"
            ),
            agent_investigation=NativeSessionDispatch(
                session_id="session-1",
                runtime="codex",
                capability="capability-token",
                capability_id="capability-1",
                source_artifact=SourceArtifact(b"test source archive", manifest_digest="0" * 64),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    )

    assert seen["url"] == "http://agent-host-codex:8010/v1/investigations"
    assert seen["json"] == {"envelope": "signed"}
    assert seen["timeout"] == NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS
    assert report.findings[0].fingerprint
    assert report.prompt_tokens == 12


def test_native_report_applies_shared_line_confidence_risk_and_fingerprint_rules(monkeypatch):
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
    payload = {
        "summary": "The guard was removed.",
        "risk_score": 1,
        "findings": [
            {**valid, "line": 2, "confidence": 0.99},
            {**valid, "confidence": 0.70},
            {**valid, "confidence": 0.90},
            valid,
        ],
    }

    report = _report_from_runner(payload, ReviewRequest(diff_text=DIFF))

    assert len(report.findings) == 1
    assert report.findings[0].title == valid["title"]
    assert report.findings[0].fingerprint == fingerprint(
        CandidateFinding.model_validate(valid), valid["title"]
    )
    assert report.risk_score == 7
    assert report.confidence_score == 2


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
        ReviewRequest(diff_text=DIFF, policy=policy),
    )

    assert report.skip_reason == "all_files_disabled"
    assert not report.publication_enabled
    assert not report.inline_comments_enabled


def test_startup_validates_only_the_selected_review_runtime(monkeypatch):
    checked = []

    def get(url, *, timeout):
        checked.append((url, timeout))
        return _Response({"state": "ready"})

    monkeypatch.setenv("REVIEW_AGENT", "codex")
    monkeypatch.setattr("service.review.agent_client.httpx.get", get)

    validate_agent_clients()

    assert checked == [("http://agent-host-codex:8010/v1/status", 5)]
