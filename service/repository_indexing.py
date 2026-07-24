"""Provider-neutral exact-commit repository indexing requests."""

from __future__ import annotations

import os
from datetime import datetime

from service.repositories import RegisteredRepository
from service.scm import PushEvent, normalize_base_url

ZERO_COMMIT = "0" * 40


def repository_api_base_url(repository: RegisteredRepository) -> str:
    if repository.scm_provider == "github":
        primary = normalize_base_url(
            os.environ.get("GITHUB_WEB_URL", "https://github.com"),
            field_name="GITHUB_WEB_URL",
        )
        if repository.scm_base_url == primary:
            configured = os.environ.get("GITHUB_API_URL")
            if configured:
                return configured
        if repository.scm_base_url == "https://github.com":
            return "https://api.github.com"
        return f"{repository.scm_base_url}/api/v3"
    primary = normalize_base_url(
        os.environ.get("GITLAB_WEB_URL", "https://gitlab.com"),
        field_name="GITLAB_WEB_URL",
    )
    if repository.scm_base_url == primary:
        configured = os.environ.get("GITLAB_API_URL")
        if configured:
            return configured
    return f"{repository.scm_base_url}/api/v4"


def repository_index_event(
    repository: RegisteredRepository,
    *,
    commit_sha: str,
    requested_at: datetime,
    delivery_id: str,
) -> PushEvent:
    return PushEvent(
        provider=repository.scm_provider,
        scm_base_url=repository.scm_base_url,
        api_base_url=repository_api_base_url(repository),
        repo_full_name=repository.full_name,
        ref_name=f"refs/heads/{repository.default_branch}",
        default_branch=repository.default_branch,
        before_sha=repository.last_fetched_sha or ZERO_COMMIT,
        after_sha=commit_sha,
        pushed_at=requested_at.isoformat(),
        delivery_id=delivery_id,
    )
