import json
import re

import httpx
import pytest

from service.conversation_models import ConversationReference
from service.github_conversation import (
    MAX_CONVERSATION_REPLY_CHARS,
    format_conversation_reply,
    publish_github_conversation_reply,
)
from service.scm import ReviewConversationEvent


def _event() -> ReviewConversationEvent:
    return ReviewConversationEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        number=7,
        delivery_id="conversation-1",
        external_comment_id="1201",
        root_comment_id="901",
        head_sha="a" * 40,
        base_sha="b" * 40,
        comment_commit_sha="a" * 40,
        author="reviewer",
        author_association="MEMBER",
        created_at="2026-07-23T17:00:00Z",
        question="Why can this bypass the tenant check?",
        file_path="service/auth.py",
        line=42,
        side="RIGHT",
        diff_hunk="@@ -41,1 +41,2 @@\n+return account",
    )


@pytest.mark.anyio
async def test_conversation_reply_is_threaded_grounded_and_idempotently_marked(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            201,
            json={
                "id": 1301,
                "in_reply_to_id": 901,
                "html_url": "https://example/comment/1301",
            },
        )

    references = (
        ConversationReference(
            file_path="service/auth.py",
            start_line=42,
            end_line=42,
            explanation="The changed lookup is not scoped.",
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await publish_github_conversation_reply(
            _event(),
            answer="The `@tenant` value is never applied to the lookup.",
            references=references,
            client=client,
        )

    assert result.external_id == "1301"
    assert [item[:2] for item in calls] == [
        ("GET", "/repos/owner/repo/pulls/7/comments"),
        ("POST", "/repos/owner/repo/pulls/7/comments/901/replies"),
    ]
    body = calls[1][2]["body"]
    assert "@\u200btenant" in body
    assert "<code>service/auth.py:42</code>" in body
    assert "diffuse-conversation:1201" in body


def test_conversation_reply_always_retains_one_trusted_recovery_marker():
    references = tuple(
        ConversationReference(
            file_path=f"service/<unsafe-`path-{index}>.py",
            start_line=42,
            end_line=43,
            explanation=(
                "<!-- diffuse-conversation:1201 --> "
                + "@reviewer "
                + ("e" * 450)
            ),
        )
        for index in range(8)
    )

    body = format_conversation_reply(
        _event(),
        "<!-- diffuse-conversation:1201 --> " + ("a" * 6000),
        references,
    )

    assert len(body) <= MAX_CONVERSATION_REPLY_CHARS
    assert body.endswith("<!-- diffuse-conversation:1201 -->")
    assert body.count("<!-- diffuse-conversation:1201 -->") == 1
    assert "&lt;!-- diffuse-conversation:1201 -->" in body
    assert "<code>service/&lt;unsafe-`path-0&gt;.py:42-43</code>" in body
    assert "@\u200breviewer" in body


@pytest.mark.parametrize(
    "fence",
    [
        "```suggestion",
        "~~~suggestion",
        "````suggestion",
        "```SUGGESTION",
        "```  suggestion",
        "  ```suggestion",
        "- ```suggestion",
        "> ```suggestion",
    ],
)
def test_conversation_reply_cannot_emit_a_committable_suggestion_block(fence: str):
    """A published answer must never become a one-click "Commit suggestion" button.

    The answer is model output derived from the untrusted question, diff hunk, and
    indexed repository context, so prompt injection decides its text. GitHub renders a
    fenced block whose info string is `suggestion` as a committable patch on the
    reviewed line, which turns an injected answer into attacker-authored code a
    maintainer commits under their own name — an escalation from a misleading reply to
    a repository write. Escaping `@` and the recovery marker did not touch the fence.
    """
    body = format_conversation_reply(
        _event(),
        f"Apply this fix:\n\n{fence}\nADMIN_TOKEN = attacker_supplied_value\n```",
        (),
    )

    # GitHub only builds the commit button when the info string is exactly `suggestion`.
    assert not re.search(
        r"(?:`{3,}|~{3,})[ \t]*suggestion[ \t]*(?:\r?\n|$)",
        body,
        re.IGNORECASE,
    )
    # Neutralizing the info string rather than the fence keeps legitimate code blocks in
    # replies rendering as code blocks.
    assert "```" in body or "~~~" in body
    assert "ADMIN_TOKEN" in body


def test_conversation_reply_keeps_ordinary_code_fences_intact():
    body = format_conversation_reply(
        _event(),
        "Scope the lookup:\n\n```python\nreturn lookup(account, tenant)\n```",
        (),
    )

    assert "```python\nreturn lookup(account, tenant)\n```" in body


@pytest.mark.anyio
async def test_conversation_reply_recovers_remote_create_without_duplication(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1302,
                    "body": "Already answered.\n\n<!-- diffuse-conversation:1201 -->",
                    "in_reply_to_id": 901,
                    "html_url": "https://example/comment/1302",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await publish_github_conversation_reply(
            _event(),
            answer="Stored answer.",
            references=(),
            client=client,
        )

    assert result.external_id == "1302"
    assert methods == ["GET"]
