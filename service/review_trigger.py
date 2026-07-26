"""Provider-neutral current-state fetch for manual review triggers."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from service.github import fetch_manual_pull_request_event
from service.gitlab import fetch_manual_gitlab_merge_request_event
from service.review_interaction import ManualReviewRequest
from service.scm import PullRequestEvent

ManualReviewFetcher = Callable[..., Awaitable[PullRequestEvent]]


def github_api_base_url(scm_base_url: str) -> str:
    configured_web_url = os.environ.get(
        "GITHUB_WEB_URL",
        "https://github.com",
    ).rstrip("/")
    normalized = scm_base_url.rstrip("/")
    if normalized == configured_web_url:
        # Only an explicit GITHUB_API_URL may override the host-derived endpoint: defaulting
        # to api.github.com here would send an enterprise token to public GitHub.
        configured_api_url = os.environ.get("GITHUB_API_URL")
        if configured_api_url:
            return configured_api_url.rstrip("/")
    if normalized == "https://github.com":
        return "https://api.github.com"
    return f"{normalized}/api/v3"


def gitlab_api_base_url(scm_base_url: str) -> str:
    configured_web_url = os.environ.get(
        "GITLAB_WEB_URL",
        "https://gitlab.com",
    ).rstrip("/")
    if scm_base_url.rstrip("/") == configured_web_url:
        return os.environ.get(
            "GITLAB_API_URL",
            f"{configured_web_url}/api/v4",
        ).rstrip("/")
    return f"{scm_base_url.rstrip('/')}/api/v4"


async def fetch_current_manual_review_event(
    *,
    target: dict[str, object],
    pull_request_number: int,
    requested_by: str,
    requested_at: str,
    trigger_source: str,
    trigger_key: str,
    branch: str | None = None,
    github_fetch: ManualReviewFetcher = fetch_manual_pull_request_event,
    gitlab_fetch: ManualReviewFetcher = fetch_manual_gitlab_merge_request_event,
) -> PullRequestEvent:
    if branch is not None and branch != target["headBranch"]:
        raise ValueError("Requested branch does not match the pull-request head")
    request = ManualReviewRequest(
        repo_full_name=str(target["name"]),
        number=pull_request_number,
        trigger_id=f"{trigger_source}:{trigger_key}",
        requested_by=requested_by,
        requested_at=requested_at,
    )
    provider = str(target["remote"])
    scm_base_url = str(target["remoteUrl"])
    delivery_id = f"{trigger_source}-{trigger_key}"
    if provider == "github":
        event = await github_fetch(
            request,
            delivery_id=delivery_id,
            scm_base_url=scm_base_url,
            api_base_url=github_api_base_url(scm_base_url),
        )
    elif provider == "gitlab":
        event = await gitlab_fetch(
            request,
            delivery_id=delivery_id,
            scm_base_url=scm_base_url,
            api_base_url=gitlab_api_base_url(scm_base_url),
        )
    else:
        raise ValueError("Review triggering requires GitHub or GitLab")
    if branch is not None and branch != event.head_branch:
        raise ValueError("Requested branch does not match the current PR/MR head")
    return event
