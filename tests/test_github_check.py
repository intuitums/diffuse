import json

import httpx
import pytest

from service.github_check import (
    complete_github_check_run,
    ensure_github_check_run,
    review_check_conclusion,
)
from service.review_models import Category, ReviewFinding, ReviewReport, Severity
from service.scm import PullRequestEvent


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 7,
            "web_url": "https://github.com/owner/repo/pull/7",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
        }
    )


def _report(*, severity: Severity = Severity.HIGH, side: str = "RIGHT") -> ReviewReport:
    return ReviewReport(
        summary="One actionable issue was found for @maintainer.",
        risk_score=7,
        confidence_score=2,
        findings=[
            ReviewFinding(
                fingerprint="f" * 64,
                title="Validate the trust boundary",
                body="The new code accepts untrusted input from @attacker.",
                severity=severity,
                category=Category.SECURITY,
                confidence=0.91,
                file_path="app.py",
                line=12,
                side=side,
                evidence="The changed call forwards user input directly.",
                suggested_fix="Validate the value first.",
            )
        ],
        diff_file_count=2,
        reviewed_file_count=2,
        context_chunk_count=1,
        prompt_tokens=100,
        completion_tokens=20,
    )


@pytest.mark.anyio
async def test_check_run_recovers_remote_creation_by_external_key(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {
                        "id": 91,
                        "external_id": "diffuse-review-run:42",
                        "html_url": "https://example/check/91",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        check_run = await ensure_github_check_run(
            _event(),
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert methods == ["GET"]
    assert check_run.external_id == "91"
    assert check_run.external_url == "https://example/check/91"


@pytest.mark.anyio
async def test_check_run_creation_is_commit_pinned(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"check_runs": []})
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 92, "html_url": "https://example/check/92"})

    event = _event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ensure_github_check_run(
            event,
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert len(payloads) == 1
    assert payloads[0]["name"] == "Diffuse code review"
    assert payloads[0]["head_sha"] == event.head_sha
    assert payloads[0]["status"] == "in_progress"
    assert payloads[0]["external_id"] == "diffuse-review-run:42"


@pytest.mark.anyio
async def test_check_completion_publishes_deterministic_conclusion_and_annotations(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 92})

    report = _report()
    assert review_check_conclusion(report, ("critical", "high")) == "failure"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await complete_github_check_run(
            _event(),
            external_id="92",
            conclusion="failure",
            blocking_severities=("critical", "high"),
            report=report,
            client=client,
        )

    payload = payloads[0]
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "failure"
    assert payload["output"]["annotations"][0]["path"] == "app.py"
    assert payload["output"]["annotations"][0]["annotation_level"] == "failure"
    assert payload["output"]["annotations"][0]["start_line"] == 12
    assert "Security vulnerability" in payload["output"]["annotations"][0]["title"]
    assert "Security vulnerability" in payload["output"]["annotations"][0]["message"]
    assert "Confidence: **2/5**" in payload["output"]["summary"]
    assert "@\u200bmaintainer" in payload["output"]["summary"]
    assert "@\u200battacker" in payload["output"]["annotations"][0]["message"]


def test_nonblocking_and_deleted_line_findings_do_not_fail_or_annotate():
    report = _report(severity=Severity.MEDIUM, side="LEFT")

    assert review_check_conclusion(report, ("critical", "high")) == "success"


def test_unresolved_prior_finding_keeps_latest_check_blocked():
    current = _report(severity=Severity.MEDIUM)
    unresolved = _report(severity=Severity.HIGH).findings[0]

    assert (
        review_check_conclusion(
            current,
            ("critical", "high"),
            unresolved_findings=(unresolved,),
        )
        == "failure"
    )
