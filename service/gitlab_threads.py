"""Idempotent GitLab merge-request discussion state synchronization."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import httpx

from service.finding_store import PublishedThreadOperation, ThreadOperationHandle
from service.gitlab_review import MAX_RESPONSE_BYTES, _headers, _merge_request_path
from service.scm import PullRequestEvent

DISCUSSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


def _discussion_url(
    event: PullRequestEvent,
    discussion_id: str,
) -> str:
    return (
        f"{_merge_request_path(event)}/discussions/"
        f"{quote(discussion_id, safe='')}"
    )


def _operation_marker(operation: ThreadOperationHandle) -> str:
    return (
        f"<!-- diffuse-thread-operation:{operation.lineage_event_id}:"
        f"{operation.kind} -->"
    )


def _operation_body(
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
) -> str:
    status = (
        "✅ Diffuse marked this finding as addressed"
        if operation.kind == "address"
        else "♻️ Diffuse detected this finding again and reopened the thread"
    )
    return (
        f"{status} at `{event.head_sha[:12]}`.\n\n"
        f"{_operation_marker(operation)}"
    )


def _discussion_notes(value: object, discussion_id: str) -> list[dict]:
    if not isinstance(value, dict) or value.get("id") != discussion_id:
        raise RuntimeError("GitLab returned the wrong merge-request discussion")
    notes = value.get("notes")
    if (
        not isinstance(notes, list)
        or not notes
        or any(not isinstance(note, dict) for note in notes)
    ):
        raise RuntimeError("GitLab returned an invalid merge-request discussion")
    return notes


async def _get_discussion(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    discussion_id: str,
) -> dict:
    response = await client.get(
        _discussion_url(event, discussion_id),
        headers=_headers(),
    )
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab discussion response exceeds Diffuse's size limit")
    value = response.json()
    _discussion_notes(value, discussion_id)
    return value


def _resolved(notes: list[dict]) -> bool:
    root = notes[0]
    resolvable = root.get("resolvable")
    resolved = root.get("resolved")
    if resolvable is not True or not isinstance(resolved, bool):
        raise RuntimeError("GitLab finding discussion is not resolvable")
    return resolved


def _existing_reply(
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
    notes: list[dict],
) -> PublishedThreadOperation | None:
    marker = _operation_marker(operation)
    for note in notes[1:]:
        body = note.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        note_id = note.get("id")
        if note_id is None:
            raise RuntimeError("GitLab discussion reply has no identifier")
        external_url = note.get("url")
        if not isinstance(external_url, str):
            external_url = f"{event.web_url}#note_{note_id}"
        return PublishedThreadOperation(
            external_reply_id=str(note_id),
            external_reply_url=external_url,
            thread_node_id=operation.thread_node_id or "",
        )
    return None


async def _set_resolution(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    discussion_id: str,
    *,
    resolved: bool,
) -> None:
    response = await client.put(
        _discussion_url(event, discussion_id),
        headers=_headers(),
        data={"resolved": "true" if resolved else "false"},
    )
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab discussion response exceeds Diffuse's size limit")
    notes = _discussion_notes(response.json(), discussion_id)
    if _resolved(notes) is not resolved:
        raise RuntimeError("GitLab did not apply the requested discussion state")


async def _create_reply(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
    discussion_id: str,
) -> PublishedThreadOperation:
    response = await client.post(
        f"{_discussion_url(event, discussion_id)}/notes",
        headers=_headers(),
        data={"body": _operation_body(event, operation)},
    )
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab discussion reply exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitLab returned an invalid discussion reply")
    note_id = value["id"]
    external_url = value.get("url")
    if not isinstance(external_url, str):
        external_url = f"{event.web_url}#note_{note_id}"
    return PublishedThreadOperation(
        external_reply_id=str(note_id),
        external_reply_url=external_url,
        thread_node_id=discussion_id,
    )


async def apply_gitlab_thread_operation(
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
    *,
    client: httpx.AsyncClient | None = None,
) -> PublishedThreadOperation:
    if event.provider != "gitlab":
        raise ValueError("GitLab thread publisher received a non-GitLab event")
    if operation.kind not in {"address", "reopen"}:
        raise ValueError("Invalid finding-thread operation")
    discussion_id = operation.thread_node_id or ""
    if not DISCUSSION_ID_PATTERN.fullmatch(discussion_id):
        raise ValueError("GitLab finding thread has no valid discussion ID")
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")

    async def apply(active_client: httpx.AsyncClient) -> PublishedThreadOperation:
        value = await _get_discussion(active_client, event, discussion_id)
        notes = _discussion_notes(value, discussion_id)
        is_resolved = _resolved(notes)
        existing = _existing_reply(event, operation, notes)

        if operation.kind == "reopen" and is_resolved:
            await _set_resolution(
                active_client,
                event,
                discussion_id,
                resolved=False,
            )
            is_resolved = False
        if existing is None:
            existing = await _create_reply(
                active_client,
                event,
                operation,
                discussion_id,
            )
        if operation.kind == "address" and not is_resolved:
            await _set_resolution(
                active_client,
                event,
                discussion_id,
                resolved=True,
            )
        return PublishedThreadOperation(
            external_reply_id=existing.external_reply_id,
            external_reply_url=existing.external_reply_url,
            thread_node_id=discussion_id,
        )

    if client is not None:
        return await apply(client)
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await apply(owned_client)
