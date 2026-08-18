"""Idempotent GitHub review-thread state synchronization."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from diffuse.database.finding import (
    PublishedThreadOperation,
    ThreadOperationHandle,
)
from diffuse.github.api import GITHUB_API_VERSION
from diffuse.github.app import github_token
from diffuse.repository.scm import (
    PullRequestEvent,
    normalize_base_url,
    scm_api_timeout_seconds,
)

MAX_THREAD_PAGES = 20
MAX_COMMENT_PAGES = 20


@dataclass(frozen=True)
class GitHubThread:
    node_id: str
    is_resolved: bool


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
        "User-Agent": "diffuse-finding-thread",
    }


def _repository_api_path(event: PullRequestEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}"
    )


def _comments_url(event: PullRequestEvent) -> str:
    return f"{_repository_api_path(event)}/pulls/{event.number}/comments"


def _graphql_url(event: PullRequestEvent) -> str:
    configured = os.environ.get("GITHUB_GRAPHQL_URL")
    if configured:
        return normalize_base_url(configured, field_name="GITHUB_GRAPHQL_URL")
    if event.api_base_url == "https://api.github.com":
        return "https://api.github.com/graphql"
    parsed = urlsplit(event.api_base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v3"):
        path = f"{path[:-len('/api/v3')]}/api/graphql"
    else:
        path = f"{path}/graphql"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


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


async def _find_existing_reply(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
) -> PublishedThreadOperation | None:
    marker = _operation_marker(operation)
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
            body = comment.get("body") if isinstance(comment, dict) else None
            if (
                not isinstance(body, str)
                or marker not in body
                or comment.get("in_reply_to_id")
                != int(operation.root_comment_id)
            ):
                continue
            external_id = comment.get("id")
            if external_id is None:
                raise RuntimeError("GitHub thread reply is missing its identifier")
            return PublishedThreadOperation(
                external_reply_id=str(external_id),
                external_reply_url=comment.get("html_url"),
                thread_node_id=operation.thread_node_id or "",
            )
        if len(value) < 100:
            break
    return None


async def _find_thread(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    root_comment_id: int,
) -> GitHubThread:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    query = """
        query DiffuseReviewThreads(
            $owner: String!,
            $repository: String!,
            $number: Int!,
            $after: String
        ) {
            repository(owner: $owner, name: $repository) {
                pullRequest(number: $number) {
                    reviewThreads(first: 100, after: $after) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            id
                            isResolved
                            comments(first: 1) {
                                nodes {
                                    databaseId
                                }
                            }
                        }
                    }
                }
            }
        }
    """
    after: str | None = None
    for _page in range(MAX_THREAD_PAGES):
        response = await client.post(
            _graphql_url(event),
            headers=_headers(),
            json={
                "query": query,
                "variables": {
                    "owner": owner,
                    "repository": repository,
                    "number": event.number,
                    "after": after,
                },
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("errors"):
            raise RuntimeError("GitHub GraphQL thread query failed")
        try:
            connection = value["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("GitHub returned an invalid review-thread response") from error
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise RuntimeError("GitHub returned an invalid review-thread connection")
        for thread in nodes:
            try:
                comment_nodes = thread["comments"]["nodes"]
                if not isinstance(comment_nodes, list) or any(
                    not isinstance(comment, dict) for comment in comment_nodes
                ):
                    raise TypeError
                matches = any(
                    int(comment["databaseId"]) == root_comment_id
                    for comment in comment_nodes
                    if comment.get("databaseId") is not None
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("GitHub returned an invalid review thread") from error
            if matches:
                node_id = thread.get("id")
                is_resolved = thread.get("isResolved")
                if not isinstance(node_id, str) or not isinstance(is_resolved, bool):
                    raise RuntimeError("GitHub review thread has invalid state")
                return GitHubThread(node_id=node_id, is_resolved=is_resolved)
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not isinstance(after, str) or not after:
            raise RuntimeError("GitHub review-thread pagination cursor is invalid")
    raise RuntimeError("GitHub review thread was not found for the Diffuse comment")


async def _set_thread_resolution(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    *,
    thread_node_id: str,
    resolved: bool,
) -> None:
    mutation_name = "resolveReviewThread" if resolved else "unresolveReviewThread"
    mutation = f"""
        mutation DiffuseThreadState($threadId: ID!) {{
            {mutation_name}(input: {{threadId: $threadId}}) {{
                thread {{
                    id
                    isResolved
                }}
            }}
        }}
    """
    response = await client.post(
        _graphql_url(event),
        headers=_headers(),
        json={
            "query": mutation,
            "variables": {"threadId": thread_node_id},
        },
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("errors"):
        raise RuntimeError("GitHub GraphQL thread mutation failed")
    try:
        thread = value["data"][mutation_name]["thread"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub returned an invalid thread mutation") from error
    if thread.get("id") != thread_node_id or thread.get("isResolved") is not resolved:
        raise RuntimeError("GitHub did not apply the requested review-thread state")


async def apply_github_thread_operation(
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
    *,
    client: httpx.AsyncClient | None = None,
) -> PublishedThreadOperation:
    if event.provider != "github":
        raise ValueError("GitHub thread publisher received a non-GitHub event")
    if operation.kind not in {"address", "reopen"}:
        raise ValueError("Invalid finding-thread operation")
    if not operation.root_comment_id.isdigit():
        raise ValueError("GitHub root review-comment ID must be numeric")
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _apply_with_client(owned_client, event, operation)
    return await _apply_with_client(client, event, operation)


async def _apply_with_client(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    operation: ThreadOperationHandle,
) -> PublishedThreadOperation:
    root_comment_id = int(operation.root_comment_id)
    existing_reply = await _find_existing_reply(client, event, operation)
    thread = await _find_thread(client, event, root_comment_id)

    if operation.kind == "reopen" and thread.is_resolved:
        await _set_thread_resolution(
            client,
            event,
            thread_node_id=thread.node_id,
            resolved=False,
        )
        thread = GitHubThread(node_id=thread.node_id, is_resolved=False)

    if existing_reply is None:
        response = await client.post(
            _comments_url(event),
            headers=_headers(),
            json={
                "body": _operation_body(event, operation),
                "in_reply_to": root_comment_id,
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("id") is None:
            raise RuntimeError("GitHub returned an invalid review-thread reply")
        existing_reply = PublishedThreadOperation(
            external_reply_id=str(value["id"]),
            external_reply_url=value.get("html_url"),
            thread_node_id=thread.node_id,
        )

    if operation.kind == "address" and not thread.is_resolved:
        await _set_thread_resolution(
            client,
            event,
            thread_node_id=thread.node_id,
            resolved=True,
        )

    return PublishedThreadOperation(
        external_reply_id=existing_reply.external_reply_id,
        external_reply_url=existing_reply.external_reply_url,
        thread_node_id=thread.node_id,
    )
