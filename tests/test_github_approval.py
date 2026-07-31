import json

import httpx
import pytest

from service.auto_approval import AutoApprovalDecision, AutoApprovalRisk
from service.github.approval import (
    ApprovalNotCurrentError,
    publish_github_approval,
)
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
            "author": "octocat",
            "base_branch": "main",
            "head_branch": "docs",
            "is_draft": False,
            "labels": (),
            "title": "Clarify docs",
            "description": "",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 1,
        }
    )


def _decision() -> AutoApprovalDecision:
    return AutoApprovalDecision(
        eligible=True,
        reason_code="approved",
        message="All checks passed.",
        risk_level=AutoApprovalRisk.LOW,
        risk_ceiling=AutoApprovalRisk.LOW,
        changed_paths=("docs/guide.md",),
        changed_file_count=1,
        changed_line_count=2,
        diff_chars=120,
    )


@pytest.mark.anyio
async def test_github_approval_is_commit_pinned_and_explains_decision(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []
    methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "draft": False,
                    "head": {"sha": _event().head_sha},
                },
            )
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": 501,
                "state": "APPROVED",
                "html_url": "https://example/review/501",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_approval(
            _event(),
            review_run_id=42,
            decision=_decision(),
            client=client,
        )

    assert [method for method, _path in methods] == ["GET", "GET", "POST"]
    assert payloads[0]["event"] == "APPROVE"
    assert payloads[0]["commit_id"] == _event().head_sha
    assert "`low`-risk" in payloads[0]["body"]
    assert f"<!-- diffuse-auto-approval:42:{_event().head_sha} -->" in (
        payloads[0]["body"]
    )
    assert published.external_id == "501"


@pytest.mark.anyio
async def test_github_approval_recovers_existing_remote_review(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    marker = f"<!-- diffuse-auto-approval:42:{_event().head_sha} -->"
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[
                {
                    "id": 502,
                    "state": "APPROVED",
                    "body": marker,
                    "html_url": "https://example/review/502",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_approval(
            _event(),
            review_run_id=42,
            decision=_decision(),
            client=client,
        )

    assert methods == ["GET"]
    assert published.external_id == "502"


@pytest.mark.anyio
async def test_github_approval_fails_closed_when_head_changes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "state": "open",
                "draft": False,
                "head": {"sha": "c" * 40},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApprovalNotCurrentError) as raised:
            await publish_github_approval(
                _event(),
                review_run_id=42,
                decision=_decision(),
                client=client,
            )

    assert raised.value.code == "head_changed"


@pytest.mark.anyio
async def test_github_approval_recovers_ambiguous_create(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    marker = f"<!-- diffuse-auto-approval:42:{_event().head_sha} -->"
    review_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_reads
        if request.method == "GET" and request.url.path.endswith("/reviews"):
            review_reads += 1
            if review_reads == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"id": 503, "state": "APPROVED", "body": marker}],
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "draft": False,
                    "head": {"sha": _event().head_sha},
                },
            )
        return httpx.Response(422, json={"message": "already submitted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_approval(
            _event(),
            review_run_id=42,
            decision=_decision(),
            client=client,
        )

    assert review_reads == 2
    assert published.external_id == "503"
