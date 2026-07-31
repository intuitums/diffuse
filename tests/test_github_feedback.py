import httpx
import pytest

from service.github.feedback import (
    MAX_REACTION_PAGES,
    fetch_github_review_reactions,
)
from service.scm import FeedbackSyncEvent


def _event() -> FeedbackSyncEvent:
    return FeedbackSyncEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        number=7,
        root_comment_id="901",
        generation=1,
        base_sha="b" * 40,
        head_sha="a" * 40,
    )


@pytest.mark.anyio
async def test_feedback_reader_keeps_only_authorized_thumb_reactions(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/reactions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "content": "+1",
                        "created_at": "2026-07-23T18:00:00Z",
                        "user": {"login": "member", "type": "User"},
                    },
                    {
                        "id": 12,
                        "content": "-1",
                        "created_at": "2026-07-23T18:01:00Z",
                        "user": {"login": "stranger", "type": "User"},
                    },
                    {
                        "id": 13,
                        "content": "heart",
                        "created_at": "2026-07-23T18:02:00Z",
                        "user": {"login": "member", "type": "User"},
                    },
                    {
                        "id": 14,
                        "content": "-1",
                        "created_at": "2026-07-23T18:03:00Z",
                        "user": {"login": "automation", "type": "Bot"},
                    },
                ],
            )
        if request.url.path.endswith("/collaborators/member"):
            return httpx.Response(204)
        if request.url.path.endswith("/collaborators/stranger"):
            return httpx.Response(404)
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reactions = await fetch_github_review_reactions(
            _event(),
            client=client,
        )

    assert [(item.external_id, item.content) for item in reactions] == [("11", "+1")]
    assert requests[0].url.path == "/repos/owner/repo/pulls/comments/901/reactions"
    assert {request.url.path for request in requests[1:]} == {
        "/repos/owner/repo/collaborators/member",
        "/repos/owner/repo/collaborators/stranger",
    }
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)


@pytest.mark.anyio
async def test_feedback_reader_rejects_malformed_thumb_reaction(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "content": "+1",
                        "created_at": "2026-07-23T18:00:00Z",
                        "user": None,
                    }
                ],
            )
        )
    ) as client:
        with pytest.raises(RuntimeError, match="no actor"):
            await fetch_github_review_reactions(_event(), client=client)


@pytest.mark.anyio
async def test_feedback_reader_fails_closed_at_pagination_cap(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json=[
                {
                    "id": (pages[-1] * 1000) + index,
                    "content": "heart",
                    "created_at": "2026-07-23T18:00:00Z",
                    "user": {"login": "member", "type": "User"},
                }
                for index in range(100)
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="pagination limit"):
            await fetch_github_review_reactions(_event(), client=client)

    assert pages == list(range(1, MAX_REACTION_PAGES + 1))
