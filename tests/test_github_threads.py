import json

import httpx
import pytest

from service.finding_store import ThreadOperationHandle
from service.github_threads import apply_github_thread_operation
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
            "action": "synchronize",
            "head_sha": "c" * 40,
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
        root_comment_node_id="PRRC_101",
        thread_node_id=None,
    )


def _thread_query_response(*, resolved: bool) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                        "nodes": [
                            {
                                "id": "PRRT_1",
                                "isResolved": resolved,
                                "comments": {
                                    "nodes": [{"databaseId": 101}]
                                },
                            }
                        ],
                    }
                }
            }
        }
    }


@pytest.mark.anyio
async def test_address_operation_replies_then_resolves_thread(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/graphql":
            if "query DiffuseReviewThreads" in payload["query"]:
                return httpx.Response(
                    200,
                    json=_thread_query_response(resolved=False),
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "id": "PRRT_1",
                                "isResolved": True,
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            201,
            json={
                "id": 202,
                "html_url": "https://example/comment/202",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_github_thread_operation(
            _event(),
            _operation(),
            client=client,
        )

    assert result.external_reply_id == "202"
    assert result.thread_node_id == "PRRT_1"
    assert [item[:2] for item in calls] == [
        ("GET", "/repos/owner/repo/pulls/7/comments"),
        ("POST", "/graphql"),
        ("POST", "/repos/owner/repo/pulls/7/comments"),
        ("POST", "/graphql"),
    ]
    assert calls[2][2]["in_reply_to"] == 101
    assert "diffuse-thread-operation:21:address" in calls[2][2]["body"]


@pytest.mark.anyio
async def test_address_operation_recovers_existing_reply_without_duplication(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    methods: list[tuple[str, str]] = []
    marker = "<!-- diffuse-thread-operation:21:address -->"

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 203,
                        "html_url": "https://example/comment/203",
                        "body": f"Already handled.\n\n{marker}",
                        "in_reply_to_id": 101,
                    }
                ],
            )
        return httpx.Response(
            200,
            json=_thread_query_response(resolved=True),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_github_thread_operation(
            _event(),
            _operation(),
            client=client,
        )

    assert result.external_reply_id == "203"
    assert result.thread_node_id == "PRRT_1"
    assert methods == [
        ("GET", "/repos/owner/repo/pulls/7/comments"),
        ("POST", "/graphql"),
    ]


@pytest.mark.anyio
async def test_reopen_operation_unresolves_before_replying(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    graphql_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/graphql":
            graphql_queries.append(payload["query"])
            if "query DiffuseReviewThreads" in payload["query"]:
                return httpx.Response(
                    200,
                    json=_thread_query_response(resolved=True),
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "unresolveReviewThread": {
                            "thread": {
                                "id": "PRRT_1",
                                "isResolved": False,
                            }
                        }
                    }
                },
            )
        return httpx.Response(201, json={"id": 204})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apply_github_thread_operation(
            _event(),
            _operation("reopen"),
            client=client,
        )

    assert result.external_reply_id == "204"
    assert len(graphql_queries) == 2
    assert "unresolveReviewThread" in graphql_queries[1]
