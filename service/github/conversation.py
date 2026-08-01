"""Idempotent GitHub replies for grounded Diffuse review conversations."""

from __future__ import annotations

import html
import os
import re
from urllib.parse import quote

import httpx

from service.github.api import GITHUB_API_VERSION
from service.github.app import github_token
from service.models.conversation import ConversationReference
from service.scm import (
    ReviewConversationEvent,
    scm_api_timeout_seconds,
)
from service.storage.conversation import PublishedConversationReply

MAX_COMMENT_PAGES = 20
MAX_CONVERSATION_REPLY_CHARS = 10_000
# GitHub turns a fenced block whose info string is `suggestion` into a one-click "Commit
# suggestion" button on the reviewed line. Answers are model output derived from the
# untrusted question, diff, and repository context, so a successful injection would end
# with attacker-authored code a maintainer can commit under their own name in one click.
_SUGGESTION_FENCE_PATTERN = re.compile(
    r"(?:`{3,}|~{3,})[ \t]*suggestion\b",
    re.IGNORECASE,
)


def _safe_markdown(value: str) -> str:
    # Only the `suggestion` info string is broken, not the fence itself: replies
    # legitimately quote code, and neutralizing the whole fence would mangle every one of
    # them. A zero-width space leaves the word readable and the block rendering as a
    # plain code block, and matches how `@` is already defused below. The fence is matched
    # anywhere rather than anchored to the line start, because GitHub still renders the
    # button when the opener is nested in a list item or a blockquote.
    value = _SUGGESTION_FENCE_PATTERN.sub(
        lambda match: f"{match.group(0)}\u200b",
        value,
    )
    return value.replace(
        "<!-- diffuse-conversation:",
        "&lt;!-- diffuse-conversation:",
    ).replace(
        "@",
        "@\u200b",
    )


def _reference_location(reference: ConversationReference) -> str:
    location = (
        f"{reference.file_path}:{reference.start_line}"
        f"{f'-{reference.end_line}' if reference.end_line != reference.start_line else ''}"
    )
    return f"<code>{html.escape(location)}</code>"


def _marker(event: ReviewConversationEvent) -> str:
    return f"<!-- diffuse-conversation:{event.external_comment_id} -->"


def format_conversation_reply(
    event: ReviewConversationEvent,
    answer: str,
    references: tuple[ConversationReference, ...],
) -> str:
    parts = [_safe_markdown(answer)]
    if references:
        parts.append("**Code references**")
        parts.extend(
            f"- {_reference_location(reference)} — "
            f"{_safe_markdown(reference.explanation)}"
            for reference in references
        )
    footer = "\n\n".join(
        (
            (
                f"<sub>Answered against `{event.head_sha[:12]}` using Diffuse's "
                "available review context.</sub>"
            ),
            _marker(event),
        )
    )
    suffix = f"\n\n{footer}"
    available = MAX_CONVERSATION_REPLY_CHARS - len(suffix)
    content = "\n\n".join(parts)[:available].rstrip()
    return f"{content}{suffix}"


def _headers() -> dict[str, str]:
    token = github_token()
    if not token:
        raise RuntimeError(
            "GitHub authentication with pull-request write permission is required"
        )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-review-conversation",
    }


def _comments_url(event: ReviewConversationEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{event.number}/comments"
    )


async def _find_existing_reply(
    client: httpx.AsyncClient,
    event: ReviewConversationEvent,
) -> PublishedConversationReply | None:
    marker = _marker(event)
    for page in range(1, MAX_COMMENT_PAGES + 1):
        response = await client.get(
            _comments_url(event),
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError("GitHub returned an invalid pull-request comment list")
        for comment in value:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            comment_id = comment.get("id")
            if (
                not isinstance(body, str)
                or marker not in body
                or comment.get("in_reply_to_id") != int(event.root_comment_id)
                or str(comment_id) == event.external_comment_id
            ):
                continue
            if comment_id is None:
                raise RuntimeError("GitHub conversation reply has no identifier")
            return PublishedConversationReply(
                external_id=str(comment_id),
                external_url=comment.get("html_url"),
            )
        if len(value) < 100:
            break
    return None


async def publish_github_conversation_reply(
    event: ReviewConversationEvent,
    *,
    answer: str,
    references: tuple[ConversationReference, ...],
    client: httpx.AsyncClient | None = None,
) -> PublishedConversationReply:
    if event.provider != "github":
        raise ValueError("GitHub conversation publisher received a non-GitHub event")
    if not answer.strip():
        raise ValueError("GitHub conversation answer cannot be empty")
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _publish_with_client(
                owned_client,
                event,
                answer,
                references,
            )
    return await _publish_with_client(client, event, answer, references)


async def _publish_with_client(
    client: httpx.AsyncClient,
    event: ReviewConversationEvent,
    answer: str,
    references: tuple[ConversationReference, ...],
) -> PublishedConversationReply:
    existing = await _find_existing_reply(client, event)
    if existing:
        return existing
    url = (
        f"{_comments_url(event)}/{quote(event.root_comment_id, safe='')}/"
        "replies"
    )
    response = await client.post(
        url,
        headers=_headers(),
        json={"body": format_conversation_reply(event, answer, references)},
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitHub returned an invalid conversation reply")
    if value.get("in_reply_to_id") not in {
        None,
        int(event.root_comment_id),
    }:
        raise RuntimeError("GitHub attached the conversation reply to the wrong thread")
    return PublishedConversationReply(
        external_id=str(value["id"]),
        external_url=value.get("html_url"),
    )
