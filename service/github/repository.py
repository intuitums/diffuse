"""GitHub repository metadata used to maintain Diffuse's local identity."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from service.github.app import github_token
from service.repositories import RegisteredRepository
from service.repository_indexing import repository_api_base_url


@dataclass(frozen=True)
class GitHubRepositoryMetadata:
    """The immutable and mutable fields Diffuse needs from GitHub."""

    repository_id: int
    full_name: str
    default_branch: str


def fetch_github_repository_metadata(
    repository: RegisteredRepository,
) -> GitHubRepositoryMetadata:
    """Fetch the canonical GitHub identity for an already configured repository."""
    owner, name = repository.full_name.split("/", maxsplit=1)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "diffuse-repository-identity",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(
            f"{repository_api_base_url(repository).rstrip('/')}/repos/"
            f"{quote(owner, safe='')}/{quote(name, safe='')}",
            headers=headers,
            timeout=20,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        repository_id = payload["id"]
        full_name = payload["full_name"]
        default_branch = payload["default_branch"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Could not verify the GitHub repository identity") from error
    if (
        not isinstance(repository_id, int)
        or isinstance(repository_id, bool)
        or repository_id <= 0
        or not isinstance(full_name, str)
        or not isinstance(default_branch, str)
    ):
        raise RuntimeError("GitHub returned an invalid repository identity")
    return GitHubRepositoryMetadata(
        repository_id=repository_id,
        full_name=full_name,
        default_branch=default_branch,
    )
