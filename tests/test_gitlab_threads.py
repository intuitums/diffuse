from urllib.parse import parse_qs

import httpx
import pytest

from service.finding_store import ThreadOperationHandle
from service.gitlab_threads import apply_gitlab_thread_operation
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
            "action": "synchronize",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T18:00:00Z",
            "delivery_id": "delivery-thread",
        }
    )


def _operation(kind: str = "address") -> ThreadOperationHandle:
    return ThreadOperationHandle(
        id=12,
        lineage_event_id=21,
        kind=kind,
        idempotency_key="finding-lineage-event:21:thread-state",
        root_comment_id="101",
        root_comment_node_id=None,
        thread_node_id="discussion-1",
    )


def _discussion(*, resolved: bool, replies: list[dict] | None = None) -> dict:
    return {
        "id": "discussion-1",
        "notes": [
            {
                "id": 101,
                "body": "Finding",
                "resolvable": True,
                "resolved": resolved,
            },
            *(replies or []),
        ],
    }


@pytest.mark.anyio
async def test_address_replies_then_resolves_gitlab_discussion(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    calls: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json=_discussion(resolved=False))
        if request.method == "POST":
            return httpx.Response(201, json={"id": 202})
        return httpx.Response(200, json=_discussion(resolved=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_gitlab_thread_operation(
            _event(),
            _operation(),
            client=client,
        )

    assert result.external_reply_id == "202"
    assert result.thread_node_id == "discussion-1"
    assert [call[:2] for call in calls] == [
        (
            "GET",
            "/api/v4/projects/group/subgroup/repo/merge_requests/17/"
            "discussions/discussion-1",
        ),
        (
            "POST",
            "/api/v4/projects/group/subgroup/repo/merge_requests/17/"
            "discussions/discussion-1/notes",
        ),
        (
            "PUT",
            "/api/v4/projects/group/subgroup/repo/merge_requests/17/"
            "discussions/discussion-1",
        ),
    ]
    assert "diffuse-thread-operation:21:address" in calls[1][2]["body"][0]
    assert calls[2][2]["resolved"] == ["true"]


@pytest.mark.anyio
async def test_address_recovers_existing_reply_without_duplication(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=_discussion(
                resolved=True,
                replies=[
                    {
                        "id": 203,
                        "body": "<!-- diffuse-thread-operation:21:address -->",
                        "url": "https://gitlab.example.com/note/203",
                    }
                ],
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_gitlab_thread_operation(
            _event(),
            _operation(),
            client=client,
        )

    assert result.external_reply_id == "203"
    assert result.external_reply_url == "https://gitlab.example.com/note/203"
    assert methods == ["GET"]


@pytest.mark.anyio
async def test_reopen_unresolves_before_replying(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=_discussion(resolved=True))
        if request.method == "PUT":
            assert parse_qs(request.content.decode())["resolved"] == ["false"]
            return httpx.Response(200, json=_discussion(resolved=False))
        return httpx.Response(201, json={"id": 204})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_gitlab_thread_operation(
            _event(),
            _operation("reopen"),
            client=client,
        )

    assert result.external_reply_id == "204"
    assert methods == ["GET", "PUT", "POST"]
