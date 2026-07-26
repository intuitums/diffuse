"""Idempotent GitLab replies for grounded Diffuse review conversations."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import httpx

from service.conversation_models import ConversationReference
from service.conversation_store import PublishedConversationReply
from service.github_conversation import format_conversation_reply
from service.gitlab_review import MAX_RESPONSE_BYTES, _headers
from service.scm import ReviewConversationEvent, raise_for_provider_status

DISCUSSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


def _discussion_url(event: ReviewConversationEvent) -> str:
    project = quote(event.repo_full_name, safe="")
    discussion = quote(event.thread_id, safe="")
    return (
        f"{event.api_base_url}/projects/{project}/merge_requests/{event.number}/"
        f"discussions/{discussion}"
    )


def _marker(event: ReviewConversationEvent) -> str:
    return f"<!-- diffuse-conversation:{event.external_comment_id} -->"


async def _find_existing_reply(
    client: httpx.AsyncClient,
    event: ReviewConversationEvent,
) -> PublishedConversationReply | None:
    response = await client.get(_discussion_url(event), headers=_headers())
    raise_for_provider_status(response, provider="gitlab")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab discussion response exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict) or value.get("id") != event.thread_id:
        raise RuntimeError("GitLab returned the wrong review discussion")
    notes = value.get("notes")
    if not isinstance(notes, list):
        raise RuntimeError("GitLab returned an invalid review discussion")
    marker = _marker(event)
    for note in notes:
        if not isinstance(note, dict):
            raise RuntimeError("GitLab returned an invalid discussion note")
        body = note.get("body")
        note_id = note.get("id")
        if (
            not isinstance(body, str)
            or marker not in body
            or str(note_id) == event.external_comment_id
        ):
            continue
        if note_id is None:
            raise RuntimeError("GitLab conversation reply has no identifier")
        external_url = note.get("url")
        if not isinstance(external_url, str):
            external_url = (
                f"{event.scm_base_url}/{quote(event.repo_full_name, safe='/')}/"
                f"-/merge_requests/{event.number}#note_{note_id}"
            )
        return PublishedConversationReply(
            external_id=str(note_id),
            external_url=external_url,
        )
    return None


async def publish_gitlab_conversation_reply(
    event: ReviewConversationEvent,
    *,
    answer: str,
    references: tuple[ConversationReference, ...],
    client: httpx.AsyncClient | None = None,
) -> PublishedConversationReply:
    if event.provider != "gitlab":
        raise ValueError("GitLab conversation publisher received a non-GitLab event")
    if not DISCUSSION_ID_PATTERN.fullmatch(event.thread_id):
        raise ValueError("GitLab conversation has no valid discussion ID")
    if not answer.strip():
        raise ValueError("GitLab conversation answer cannot be empty")
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")

    async def publish(active_client: httpx.AsyncClient) -> PublishedConversationReply:
        existing = await _find_existing_reply(active_client, event)
        if existing is not None:
            return existing
        response = await active_client.post(
            f"{_discussion_url(event)}/notes",
            headers=_headers(),
            data={
                "body": format_conversation_reply(
                    event,
                    answer,
                    references,
                )
            },
        )
        raise_for_provider_status(response, provider="gitlab")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise RuntimeError(
                "GitLab conversation reply exceeds Diffuse's size limit"
            )
        value = response.json()
        if not isinstance(value, dict) or value.get("id") is None:
            raise RuntimeError("GitLab returned an invalid conversation reply")
        note_id = value["id"]
        external_url = value.get("url")
        if not isinstance(external_url, str):
            external_url = (
                f"{event.scm_base_url}/{quote(event.repo_full_name, safe='/')}/"
                f"-/merge_requests/{event.number}#note_{note_id}"
            )
        return PublishedConversationReply(
            external_id=str(note_id),
            external_url=external_url,
        )

    if client is not None:
        return await publish(client)
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await publish(owned_client)
