"""Authorized GitLab emoji reconciliation for Diffuse finding notes."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from service.feedback_models import ReviewReaction
from service.gitlab_review import MAX_RESPONSE_BYTES, _headers
from service.scm import (
    FeedbackSyncEvent,
    ProviderPaginationLimitError,
    raise_for_provider_status,
)

MAX_REACTION_PAGES = 20
REACTION_CONTENT = {
    "thumbsup": "+1",
    "+1": "+1",
    "thumbsdown": "-1",
    "-1": "-1",
}


def _project_url(event: FeedbackSyncEvent) -> str:
    return (
        f"{event.api_base_url}/projects/"
        f"{quote(event.repo_full_name, safe='')}"
    )


async def _is_project_member(
    client: httpx.AsyncClient,
    event: FeedbackSyncEvent,
    user_id: int,
) -> bool:
    response = await client.get(
        f"{_project_url(event)}/members/all/{user_id}",
        headers=_headers(),
    )
    if response.status_code == 404:
        return False
    raise_for_provider_status(response, provider="gitlab")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab member response exceeds Diffuse's size limit")
    value = response.json()
    access_level = value.get("access_level") if isinstance(value, dict) else None
    if not isinstance(access_level, int) or isinstance(access_level, bool):
        raise RuntimeError("GitLab returned invalid member metadata")
    return access_level >= 30


async def fetch_gitlab_review_reactions(
    event: FeedbackSyncEvent,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[ReviewReaction, ...]:
    if event.provider != "gitlab":
        raise ValueError("GitLab feedback reader received a non-GitLab event")
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")

    async def fetch(active_client: httpx.AsyncClient) -> tuple[ReviewReaction, ...]:
        candidates: list[tuple[ReviewReaction, int]] = []
        url = (
            f"{_project_url(event)}/merge_requests/{event.number}/notes/"
            f"{quote(event.root_comment_id, safe='')}/award_emoji"
        )
        reached_end = False
        for page in range(1, MAX_REACTION_PAGES + 1):
            response = await active_client.get(
                url,
                headers=_headers(),
                params={"per_page": 100, "page": page},
            )
            raise_for_provider_status(response, provider="gitlab")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    "GitLab emoji response exceeds Diffuse's size limit"
                )
            value = response.json()
            if not isinstance(value, list):
                raise RuntimeError("GitLab returned an invalid emoji list")
            for item in value:
                if not isinstance(item, dict):
                    raise RuntimeError("GitLab returned an invalid emoji reaction")
                content = REACTION_CONTENT.get(item.get("name"))
                if content is None:
                    continue
                user = item.get("user")
                if not isinstance(user, dict):
                    raise RuntimeError("GitLab emoji reaction has no actor")
                user_id = user.get("id")
                username = user.get("username")
                reaction_id = item.get("id")
                created_at = item.get("created_at")
                if (
                    not isinstance(user_id, int)
                    or isinstance(user_id, bool)
                    or user_id <= 0
                    or not isinstance(username, str)
                    or not username
                    or reaction_id is None
                    or not isinstance(created_at, str)
                ):
                    raise RuntimeError(
                        "GitLab emoji reaction has invalid provenance"
                    )
                candidates.append(
                    (
                        ReviewReaction(
                            external_id=str(reaction_id),
                            actor_login=username,
                            content=content,
                            created_at=created_at,
                        ),
                        user_id,
                    )
                )
            if len(value) < 100:
                reached_end = True
                break
        if not reached_end:
            raise ProviderPaginationLimitError(
                "gitlab",
                "emoji reactions",
                pages=MAX_REACTION_PAGES,
            )

        authorization: dict[int, bool] = {}
        for user_id in sorted({user_id for _reaction, user_id in candidates}):
            authorization[user_id] = await _is_project_member(
                active_client,
                event,
                user_id,
            )
        return tuple(
            reaction
            for reaction, user_id in candidates
            if authorization.get(user_id, False)
        )

    if client is not None:
        return await fetch(client)
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await fetch(owned_client)
