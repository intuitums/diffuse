from urllib.parse import parse_qs

import httpx
import pytest

from service.gitlab_conversation import publish_gitlab_conversation_reply
from service.scm import ReviewConversationEvent


def _event() -> ReviewConversationEvent:
    return ReviewConversationEvent(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        repo_full_name="group/subgroup/repo",
        number=17,
        delivery_id="note-delivery-1",
        external_comment_id="401",
        root_comment_id="202",
        head_sha="a" * 40,
        base_sha="b" * 40,
        comment_commit_sha="a" * 40,
        author="reviewer",
        author_association="COLLABORATOR",
        created_at="2026-07-23T18:30:00Z",
        question="Why is this unsafe?",
        file_path="service/read.py",
        line=12,
        side="RIGHT",
        diff_hunk="",
        thread_id="discussion-1",
    )


@pytest.mark.anyio
async def test_publish_gitlab_conversation_replies_in_same_discussion(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    payloads: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "discussion-1",
                    "notes": [
                        {"id": 202, "body": "Finding"},
                        {"id": 401, "body": "@diffuse why?"},
                    ],
                },
            )
        payloads.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"id": 402})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await publish_gitlab_conversation_reply(
            _event(),
            answer="The query omits the tenant boundary.",
            references=(),
            client=client,
        )

    assert result.external_id == "402"
    assert result.external_url == (
        "https://gitlab.example.com/group/subgroup/repo/"
        "-/merge_requests/17#note_402"
    )
    assert "The query omits the tenant boundary." in payloads[0]["body"][0]
    assert "<!-- diffuse-conversation:401 -->" in payloads[0]["body"][0]


@pytest.mark.anyio
async def test_publish_gitlab_conversation_recovers_existing_reply(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": "discussion-1",
                "notes": [
                    {"id": 202, "body": "Finding"},
                    {"id": 401, "body": "@diffuse why?"},
                    {
                        "id": 402,
                        "body": "<!-- diffuse-conversation:401 -->",
                        "url": "https://gitlab.example.com/note/402",
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await publish_gitlab_conversation_reply(
            _event(),
            answer="The query omits the tenant boundary.",
            references=(),
            client=client,
        )

    assert result.external_id == "402"
    assert result.external_url == "https://gitlab.example.com/note/402"
    assert methods == ["GET"]
