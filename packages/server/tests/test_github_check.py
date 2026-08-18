import json

import httpx
import pytest
from diffuse.github.check import (
    MAX_CHECK_ANNOTATIONS,
    complete_github_check_run,
    ensure_github_check_run,
    find_github_check_run,
    review_check_conclusion,
)
from diffuse.repository.scm import PullRequestEvent
from diffuse_protocol.review import Category, ReviewFinding, ReviewReport, Severity


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
async def test_find_github_check_run_does_not_create(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"check_runs": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        check_run = await find_github_check_run(
            _event(),
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert methods == ["GET"]
    assert check_run is None


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


def _open_lineage_finding(
    *,
    fingerprint: str = "e" * 64,
    line: int = 44,
    severity: Severity = Severity.HIGH,
) -> ReviewFinding:
    return ReviewFinding(
        fingerprint=fingerprint,
        title="Tenant scope is missing",
        body="The query is still not scoped to the caller's tenant.",
        severity=severity,
        category=Category.CORRECTNESS,
        confidence=0.88,
        file_path="db.py",
        line=line,
        side="RIGHT",
        evidence="The lookup filters on id alone.",
    )


@pytest.mark.anyio
async def test_unresolved_prior_finding_is_annotated_without_duplicates(monkeypatch):
    """An older open lineage that blocks the check must also point at its line."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 92})

    current = _report(severity=Severity.MEDIUM)
    unresolved = (current.findings[0], _open_lineage_finding())
    conclusion = review_check_conclusion(
        current,
        ("critical", "high"),
        unresolved_findings=unresolved,
    )

    assert conclusion == "failure"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await complete_github_check_run(
            _event(),
            external_id="92",
            conclusion=conclusion,
            blocking_severities=("critical", "high"),
            report=current,
            unresolved_findings=unresolved,
            client=client,
        )

    output = payloads[0]["output"]
    annotations = output["annotations"]
    assert [annotation["path"] for annotation in annotations] == ["app.py", "db.py"]
    assert annotations[0]["annotation_level"] == "warning"
    assert annotations[1]["annotation_level"] == "failure"
    assert annotations[1]["start_line"] == 44
    assert output["title"] == "Diffuse found 1 blocking finding"


@pytest.mark.anyio
async def test_check_annotations_stay_within_the_github_request_cap(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 92})

    current = _report(severity=Severity.MEDIUM)
    unresolved = tuple(
        _open_lineage_finding(fingerprint=f"{index:064x}", line=index + 1)
        for index in range(MAX_CHECK_ANNOTATIONS + 10)
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await complete_github_check_run(
            _event(),
            external_id="92",
            conclusion="failure",
            blocking_severities=("critical", "high"),
            report=current,
            unresolved_findings=unresolved,
            client=client,
        )

    assert len(payloads[0]["output"]["annotations"]) == MAX_CHECK_ANNOTATIONS


@pytest.mark.anyio
async def test_blocking_lineage_survives_the_annotation_cap(monkeypatch):
    """The finding that turned the check red must be annotated, not crowded out.

    MAX_CHECK_ANNOTATIONS is a hard GitHub-side cap. A run that emits a full cap
    of non-blocking findings would, in source order, consume every slot and leave
    the single blocking lineage that produced the `failure` conclusion with no
    annotation — the same empty-Files-tab symptom `_annotations` exists to fix.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 93})

    blocking_fingerprint = "b" * 64
    # A full cap of LOW findings in this run, plus one HIGH open lineage.
    noisy = _report(severity=Severity.LOW).model_copy(
        update={
            "findings": tuple(
                _open_lineage_finding(
                    fingerprint=f"{index:064x}",
                    line=index + 1,
                    severity=Severity.LOW,
                )
                for index in range(MAX_CHECK_ANNOTATIONS)
            )
        }
    )
    unresolved = (
        _open_lineage_finding(
            fingerprint=blocking_fingerprint,
            line=900,
            severity=Severity.HIGH,
        ),
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await complete_github_check_run(
            _event(),
            external_id="93",
            conclusion="failure",
            blocking_severities=("critical", "high"),
            report=noisy,
            unresolved_findings=unresolved,
            client=client,
        )

    annotations = payloads[0]["output"]["annotations"]
    assert len(annotations) == MAX_CHECK_ANNOTATIONS
    # It wins a slot despite arriving last, with the cap already full.
    assert 900 in [annotation["start_line"] for annotation in annotations], (
        "the blocking lineage that turned the check red was dropped by the cap"
    )
    levels = [annotation["annotation_level"] for annotation in annotations]
    assert levels.count("failure") == 1
    # Presentation order is unchanged: this run's findings still come first.
    assert annotations[0]["start_line"] == 1
    assert annotations[-1]["start_line"] == 900
