import httpx
import pytest

from service.gitlab_feedback import fetch_gitlab_review_reactions
from service.scm import FeedbackSyncEvent


def _event() -> FeedbackSyncEvent:
    return FeedbackSyncEvent(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        repo_full_name="group/subgroup/repo",
        number=17,
        root_comment_id="202",
        generation=1,
        base_sha="b" * 40,
        head_sha="a" * 40,
    )


@pytest.mark.anyio
async def test_fetch_gitlab_reactions_filters_kind_and_project_members(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/members/all/8"):
            return httpx.Response(200, json={"id": 8, "access_level": 30})
        if request.url.path.endswith("/members/all/9"):
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": 501,
                    "name": "thumbsup",
                    "user": {"id": 8, "username": "member"},
                    "created_at": "2026-07-23T18:30:00Z",
                },
                {
                    "id": 502,
                    "name": "thumbsdown",
                    "user": {"id": 9, "username": "outsider"},
                    "created_at": "2026-07-23T18:31:00Z",
                },
                {
                    "id": 503,
                    "name": "rocket",
                    "user": {"id": 8, "username": "member"},
                    "created_at": "2026-07-23T18:32:00Z",
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reactions = await fetch_gitlab_review_reactions(
            _event(),
            client=client,
        )

    assert [(item.external_id, item.actor_login, item.content) for item in reactions] == [
        ("501", "member", "+1")
    ]
    assert requests == [
        (
            "/api/v4/projects/group/subgroup/repo/merge_requests/17/"
            "notes/202/award_emoji"
        ),
        "/api/v4/projects/group/subgroup/repo/members/all/8",
        "/api/v4/projects/group/subgroup/repo/members/all/9",
    ]
