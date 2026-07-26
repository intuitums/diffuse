from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from service import gitlab_check
from service.gitlab_check import (
    complete_gitlab_check_run,
    ensure_gitlab_check_run,
)
from service.review_models import Category, ReviewFinding, ReviewReport, Severity
from service.scm import PullRequestEvent


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "group/subgroup/repo",
            "number": 17,
            "web_url": (
                "https://gitlab.example.com/group/subgroup/repo/-/merge_requests/17"
            ),
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-17",
            "author": "contributor",
            "base_branch": "main",
            "head_branch": "feature/auth",
            "is_draft": False,
            "labels": [],
            "title": "Protect tenant reads",
            "description": "",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 1,
            "state": "open",
            "source_created_at": "2026-07-23T14:00:00Z",
            "source_closed_at": "",
            "source_merged_at": "",
            "additions": 0,
            "deletions": 0,
            "source_project_id": 122,
        }
    )


def _report() -> ReviewReport:
    return ReviewReport(
        summary="One issue.",
        risk_score=7,
        findings=[
            ReviewFinding(
                fingerprint="f" * 64,
                title="Validate tenant access",
                body="The query is not tenant scoped.",
                severity=Severity.HIGH,
                category=Category.SECURITY,
                confidence=0.94,
                file_path="service/read.py",
                line=12,
                side="RIGHT",
                evidence="Only the object ID is filtered.",
            )
        ],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=1,
        prompt_tokens=100,
        completion_tokens=20,
    )


@pytest.mark.anyio
async def test_status_creation_is_head_and_source_branch_pinned(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    payloads: list[dict[str, list[str]]] = []
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        payloads.append(parse_qs(request.content.decode()))
        return httpx.Response(
            201,
            json={
                "id": 91,
                "target_url": _event().web_url,
                "status": "running",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status = await ensure_gitlab_check_run(
            _event(),
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert status.external_id == "91"
    assert payloads[0]["state"] == ["running"]
    assert payloads[0]["name"] == ["Diffuse code review"]
    assert payloads[0]["ref"] == ["feature/auth"]
    assert paths == [f"/api/v4/projects/122/statuses/{'a' * 40}"]


@pytest.mark.anyio
async def test_status_completion_maps_diffuse_conclusion_and_summary(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    payloads: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"id": 92, "status": "failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await complete_gitlab_check_run(
            _event(),
            external_id="91",
            conclusion="failure",
            blocking_severities=("critical", "high"),
            report=_report(),
            client=client,
        )

    assert payloads[0]["state"] == ["failed"]
    assert payloads[0]["name"] == ["Diffuse code review"]
    assert "1 active findings" in payloads[0]["description"][0]
    assert "1 blocking" in payloads[0]["description"][0]


@pytest.mark.anyio
async def test_status_retries_documented_concurrent_update_conflict(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(409, json={"message": "update in progress"})
        return httpx.Response(201, json={"id": 93, "status": "running"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ensure_gitlab_check_run(
            _event(),
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert attempts == 2


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_status_retries_transient_gateway_faults(monkeypatch, status_code):
    """A single 503 used to fail the whole review job instead of backing off."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setattr(gitlab_check.anyio, "sleep", AsyncMock())
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, json={"message": "try again"})
        return httpx.Response(201, json={"id": 93, "status": "running"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ensure_gitlab_check_run(
            _event(),
            external_key="diffuse-review-run:42",
            client=client,
        )

    assert attempts == 2


@pytest.mark.anyio
async def test_status_conflict_backoff_grows_and_is_jittered(monkeypatch):
    """Racing review jobs must not line their conflict retries up on one instant."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    windows: list[tuple[float, float]] = []
    monkeypatch.setattr(gitlab_check.anyio, "sleep", AsyncMock())

    def uniform(low: float, high: float) -> float:
        windows.append((low, high))
        return low

    monkeypatch.setattr(gitlab_check.random, "uniform", uniform)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "update in progress"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ensure_gitlab_check_run(
                _event(),
                external_key="diffuse-review-run:42",
                client=client,
            )

    assert windows == [(0.25, 0.5), (0.5, 1.0)]
    assert len(windows) == gitlab_check.MAX_STATUS_ATTEMPTS - 1
