import json

import httpx
import pytest

from service.approval_publication import ApprovalNotCurrentError
from service.auto_approval import AutoApprovalDecision, AutoApprovalRisk
from service.gitlab_approval import publish_gitlab_approval
from service.scm import PullRequestEvent


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "group/subgroup/repo",
            "number": 7,
            "web_url": (
                "https://gitlab.example.com/group/subgroup/repo/"
                "-/merge_requests/7"
            ),
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
            "author": "contributor",
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


def _merge_request(**overrides) -> dict:
    value = {
        "project_id": 91,
        "iid": 7,
        "web_url": (
            "https://gitlab.example.com/group/subgroup/repo/"
            "-/merge_requests/7"
        ),
        "state": "opened",
        "draft": False,
        "sha": "a" * 40,
        "detailed_merge_status": "mergeable",
    }
    value.update(overrides)
    return value


def _versions(*, patch_id_sha: str | None = "c" * 40) -> list[dict]:
    return [
        {
            "head_commit_sha": "a" * 40,
            "patch_id_sha": patch_id_sha,
        }
    ]


def _approval_state() -> dict:
    return {
        "project_id": 91,
        "iid": 7,
        "state": "opened",
        "approved_by": [
            {
                "user": {
                    "id": 41,
                    "username": "diffuse-bot",
                },
                "approved_at": "2026-07-23T19:00:00Z",
            }
        ],
    }


@pytest.mark.anyio
async def test_gitlab_approval_waits_for_synced_diff_and_posts_exact_head(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path.decode()))
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json=_versions())
        if request.url.path.endswith("/user"):
            return httpx.Response(
                200,
                json={"id": 41, "username": "diffuse-bot"},
            )
        if request.method == "POST":
            assert request.headers["private-token"] == "test-token"
            posted.append(json.loads(request.content))
            return httpx.Response(201, json=_approval_state())
        return httpx.Response(200, json=_merge_request())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_approval(
            _event(),
            review_run_id=42,
            decision=_decision(),
            client=client,
        )

    project_path = "/api/v4/projects/group%2Fsubgroup%2Frepo/merge_requests/7"
    assert calls == [
        ("GET", project_path),
        ("GET", f"{project_path}/versions"),
        ("GET", "/api/v4/user"),
        ("POST", f"{project_path}/approve"),
    ]
    assert posted == [{"sha": "a" * 40}]
    assert published.external_id == (
        f"gitlab:group/subgroup/repo:7:41:{'a' * 40}"
    )
    assert published.external_url == _event().web_url


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("merge_request", "error_code"),
    [
        (_merge_request(sha="d" * 40), "head_changed"),
        (_merge_request(state="closed"), "pull_request_closed"),
        (_merge_request(draft=True), "pull_request_draft"),
    ],
)
async def test_gitlab_approval_fails_closed_for_stale_state(
    monkeypatch,
    merge_request,
    error_code,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=merge_request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApprovalNotCurrentError) as raised:
            await publish_gitlab_approval(
                _event(),
                review_run_id=42,
                decision=_decision(),
                client=client,
            )

    assert raised.value.code == error_code


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("merge_status", "patch_id_sha"),
    [
        ("approvals_syncing", "c" * 40),
        ("mergeable", None),
    ],
)
async def test_gitlab_approval_retries_until_approval_state_is_synced(
    monkeypatch,
    merge_status,
    patch_id_sha,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json=_versions(patch_id_sha=patch_id_sha),
            )
        return httpx.Response(
            200,
            json=_merge_request(detailed_merge_status=merge_status),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="synchron"):
            await publish_gitlab_approval(
                _event(),
                review_run_id=42,
                decision=_decision(),
                client=client,
            )


@pytest.mark.anyio
async def test_gitlab_approval_recovers_existing_bot_approval_after_exact_sha_post(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.raw_path.decode()))
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json=_versions())
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 41})
        if request.url.path.endswith("/approve"):
            assert json.loads(request.content) == {"sha": "a" * 40}
            return httpx.Response(401, json={"message": "Unauthorized"})
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200, json=_approval_state())
        return httpx.Response(200, json=_merge_request())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_approval(
            _event(),
            review_run_id=42,
            decision=_decision(),
            client=client,
        )

    assert methods[-2:] == [
        (
            "POST",
            (
                "/api/v4/projects/group%2Fsubgroup%2Frepo/"
                "merge_requests/7/approve"
            ),
        ),
        (
            "GET",
            (
                "/api/v4/projects/group%2Fsubgroup%2Frepo/"
                "merge_requests/7/approvals"
            ),
        ),
    ]
    assert published.external_id.endswith(f":41:{'a' * 40}")


@pytest.mark.anyio
async def test_gitlab_approval_maps_sha_conflict_to_stale_head(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json=_versions())
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 41})
        if request.method == "POST":
            return httpx.Response(409, json={"message": "SHA does not match HEAD"})
        return httpx.Response(200, json=_merge_request())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApprovalNotCurrentError) as raised:
            await publish_gitlab_approval(
                _event(),
                review_run_id=42,
                decision=_decision(),
                client=client,
            )

    assert raised.value.code == "head_changed"
