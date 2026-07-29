"""Commit-pinned, remotely recoverable GitHub automatic approval."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from service.approval_publication import (
    ApprovalNotCurrentError,
    PublishedApproval,
)
from service.auto_approval import AutoApprovalDecision
from service.github import GITHUB_API_VERSION
from service.github_app import github_token
from service.scm import (
    PullRequestEvent,
    scm_api_timeout_seconds,
)


def _headers() -> dict[str, str]:
    token = github_token()
    if not token:
        raise RuntimeError("GitHub authentication is required to approve pull requests")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-auto-approval",
    }


def _pull_url(event: PullRequestEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{event.number}"
    )


def _reviews_url(event: PullRequestEvent) -> str:
    return f"{_pull_url(event)}/reviews"


async def _find_existing_approval(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    marker: str,
) -> PublishedApproval | None:
    for page in range(1, 21):
        response = await client.get(
            _reviews_url(event),
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        reviews = response.json()
        if not isinstance(reviews, list):
            raise RuntimeError("GitHub returned an invalid review list")
        for review in reviews:
            body = review.get("body") if isinstance(review, dict) else None
            if not isinstance(body, str) or marker not in body:
                continue
            if str(review.get("state", "")).upper() != "APPROVED":
                raise RuntimeError(
                    "GitHub review has the approval marker without approved state"
                )
            external_id = review.get("id")
            if external_id is None:
                raise RuntimeError("GitHub approval is missing its identifier")
            return PublishedApproval(
                external_id=str(external_id),
                external_url=review.get("html_url"),
            )
        if len(reviews) < 100:
            break
    return None


async def _assert_current_head(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
) -> None:
    response = await client.get(_pull_url(event), headers=_headers())
    response.raise_for_status()
    pull_request = response.json()
    try:
        current_head = pull_request["head"]["sha"]
        state = pull_request["state"]
        is_draft = pull_request["draft"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "GitHub returned invalid pull-request state"
        ) from error
    if state != "open":
        raise ApprovalNotCurrentError(
            "pull_request_closed",
            "The pull request closed before automatic approval.",
        )
    if is_draft:
        raise ApprovalNotCurrentError(
            "pull_request_draft",
            "The pull request became a draft before automatic approval.",
        )
    if current_head != event.head_sha:
        raise ApprovalNotCurrentError(
            "head_changed",
            "The pull-request head changed before automatic approval.",
        )


async def publish_github_approval(
    event: PullRequestEvent,
    *,
    review_run_id: int,
    decision: AutoApprovalDecision,
    client: httpx.AsyncClient | None = None,
) -> PublishedApproval:
    if event.provider != "github":
        raise ValueError("GitHub approval publisher received a non-GitHub event")
    if not decision.eligible:
        raise ValueError("An ineligible decision cannot publish an approval")
    marker = f"<!-- diffuse-auto-approval:{review_run_id}:{event.head_sha} -->"
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _publish_with_client(
                owned_client,
                event,
                review_run_id,
                decision,
                marker,
            )
    return await _publish_with_client(
        client,
        event,
        review_run_id,
        decision,
        marker,
    )


async def _publish_with_client(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    review_run_id: int,
    decision: AutoApprovalDecision,
    marker: str,
) -> PublishedApproval:
    existing = await _find_existing_approval(client, event, marker)
    if existing is not None:
        return existing
    await _assert_current_head(client, event)
    payload = {
        "commit_id": event.head_sha,
        "body": (
            f"Diffuse automatically approved this `{decision.risk_level.value}`-risk "
            "change after a clean review and all configured approval filters passed.\n\n"
            f"{marker}"
        ),
        "event": "APPROVE",
    }
    response = await client.post(
        _reviews_url(event),
        headers=_headers(),
        json=payload,
    )
    if response.status_code == 422:
        existing = await _find_existing_approval(client, event, marker)
        if existing is not None:
            return existing
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitHub returned an invalid created approval")
    if str(value.get("state", "")).upper() != "APPROVED":
        raise RuntimeError("GitHub did not create an approved review")
    return PublishedApproval(
        external_id=str(value["id"]),
        external_url=value.get("html_url"),
    )
