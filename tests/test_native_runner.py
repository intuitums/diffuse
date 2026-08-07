from __future__ import annotations

from datetime import UTC, datetime, timedelta

from service.review.native_runner import NativeRunnerRuntime, NativeSessionDispatch
from service.review.request import ReviewRequest


class _Response:
    status_code = 200

    def json(self):
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
                    "line": 2,
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
        return _Response()

    monkeypatch.setattr("service.review.native_runner.httpx.post", post)
    monkeypatch.setattr("service.review.native_runner.sign_dispatch", lambda _envelope: "signed")
    monkeypatch.setattr("service.review.native_runner._accept_completion", lambda *_args: None)
    report = NativeRunnerRuntime("codex").generate(
        ReviewRequest(
            diff_text="diff --git a/app.py b/app.py\n+++ b/app.py\n",
            agent_session=NativeSessionDispatch(
                session_id="session-1",
                runtime="codex",
                capability="capability-token",
                capability_id="capability-1",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    )

    assert seen["url"] == "http://agent-runner-codex:8010/v1/reviews"
    assert seen["json"] == {"envelope": "signed"}
    assert report.findings[0].fingerprint
    assert report.prompt_tokens == 12
