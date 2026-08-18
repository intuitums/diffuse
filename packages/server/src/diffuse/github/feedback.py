"""Authorized GitHub reaction reconciliation for Diffuse finding comments."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from diffuse.github.api import GITHUB_API_VERSION
from diffuse.github.app import github_token
from diffuse.github.feedback_models import ReviewReaction
from diffuse.repository.scm import (
    FeedbackSyncEvent,
    scm_api_timeout_seconds,
)

MAX_REACTION_PAGES = 20


def _headers() -> dict[str, str]:
    token = github_token()
    if not token:
        raise RuntimeError(
            "GitHub authentication with pull-request read permission is required"
        )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-review-feedback",
    }


def _repository_url(event: FeedbackSyncEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}"
    )


async def _is_collaborator(
    client: httpx.AsyncClient,
    event: FeedbackSyncEvent,
    actor_login: str,
) -> bool:
    response = await client.get(
        f"{_repository_url(event)}/collaborators/{quote(actor_login, safe='')}",
        headers=_headers(),
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return response.status_code == 204


async def fetch_github_review_reactions(
    event: FeedbackSyncEvent,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[ReviewReaction, ...]:
    if event.provider != "github":
        raise ValueError("GitHub feedback reader received a non-GitHub event")
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _fetch_with_client(owned_client, event)
    return await _fetch_with_client(client, event)


async def _fetch_with_client(
    client: httpx.AsyncClient,
    event: FeedbackSyncEvent,
) -> tuple[ReviewReaction, ...]:
    candidates: list[ReviewReaction] = []
    user_types: dict[str, str] = {}
    reactions_url = (
        f"{_repository_url(event)}/pulls/comments/"
        f"{quote(event.root_comment_id, safe='')}/reactions"
    )
    reached_end = False
    for page in range(1, MAX_REACTION_PAGES + 1):
        response = await client.get(
            reactions_url,
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError("GitHub returned an invalid review-reaction list")
        for item in value:
            if not isinstance(item, dict):
                raise RuntimeError("GitHub returned an invalid review reaction")
            content = item.get("content")
            if content not in {"+1", "-1"}:
                continue
            user = item.get("user")
            if not isinstance(user, dict):
                raise RuntimeError("GitHub review reaction has no actor")
            actor_login = user.get("login")
            actor_type = user.get("type")
            if not isinstance(actor_login, str) or not isinstance(actor_type, str):
                raise RuntimeError("GitHub review reaction has an invalid actor")
            if actor_type.casefold() == "bot":
                continue
            reaction_id = item.get("id")
            created_at = item.get("created_at")
            if reaction_id is None or not isinstance(created_at, str):
                raise RuntimeError("GitHub review reaction has invalid provenance")
            candidates.append(
                ReviewReaction(
                    external_id=str(reaction_id),
                    actor_login=actor_login,
                    content=content,
                    created_at=created_at,
                )
            )
            user_types[actor_login] = actor_type
        if len(value) < 100:
            reached_end = True
            break
    if not reached_end:
        raise RuntimeError(
            "GitHub review reactions exceed Diffuse's safe pagination limit"
        )

    authorization: dict[str, bool] = {}
    for actor_login in sorted(user_types, key=str.casefold):
        authorization[actor_login] = await _is_collaborator(
            client,
            event,
            actor_login,
        )
    return tuple(
        reaction
        for reaction in candidates
        if authorization.get(reaction.actor_login, False)
    )
