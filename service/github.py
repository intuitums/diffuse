"""GitHub webhook normalization and pull-request API access."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status

from service.review_interaction import (
    ManualReviewRequest,
    conversation_question,
    is_diffuse_generated,
    is_human_only_discussion,
    is_manual_review_trigger,
)
from service.scm import (
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
    normalize_base_url,
    raise_for_provider_status,
    scm_api_timeout_seconds,
)

GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
GITHUB_API_VERSION = "2026-03-10"
MAX_DIFF_BYTES = 2_000_000
MAX_METADATA_BYTES = 1_000_000
MANUAL_TRIGGER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret is not configured",
        )
    # Starlette decodes header bytes as latin-1 and the HTTP parsers accept
    # obs-text, so an attacker can put non-ASCII codepoints in the header;
    # hmac.compare_digest raises TypeError on those, which would surface as a
    # 500 instead of an authentication failure.
    if not signature.isascii() or not signature.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid webhook signature",
        )
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid webhook signature",
        )


def normalize_pull_request_event(
    payload: dict,
    *,
    delivery_id: str,
    action: str,
    trigger_kind: str = "automatic",
    trigger_id: str = "",
    updated_at_override: str | None = None,
    scm_base_url_override: str | None = None,
    api_base_url_override: str | None = None,
) -> PullRequestEvent:
    try:
        repo_full_name = payload["repository"]["full_name"]
        pull_request = payload["pull_request"]
        pr_number = int(pull_request["number"])
        pr_url = pull_request["html_url"]
        head_sha = pull_request["head"]["sha"]
        head_branch = pull_request["head"]["ref"]
        base_sha = pull_request["base"]["sha"]
        base_branch = pull_request["base"]["ref"]
        updated_at = updated_at_override or pull_request["updated_at"]
        author = pull_request["user"]["login"]
        is_draft = pull_request["draft"]
        title = pull_request["title"]
        description = pull_request.get("body") or ""
        changed_file_count = pull_request["changed_files"]
        raw_state = pull_request.get("state", "open")
        state = (
            "merged"
            if pull_request.get("merged") is True
            or pull_request.get("merged_at") is not None
            else raw_state
        )
        source_created_at = pull_request["created_at"]
        source_closed_at = pull_request.get("closed_at") or ""
        source_merged_at = pull_request.get("merged_at") or ""
        additions = pull_request.get("additions", 0)
        deletions = pull_request.get("deletions", 0)
        raw_labels = pull_request["labels"]
        labels = tuple(label["name"] for label in raw_labels)
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request payload",
        ) from error

    scm_base_url = normalize_base_url(
        scm_base_url_override
        or os.environ.get("GITHUB_WEB_URL", "https://github.com"),
        field_name="GITHUB_WEB_URL",
    )
    api_base_url = normalize_base_url(
        api_base_url_override
        or os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        field_name="GITHUB_API_URL",
    )
    valid = (
        isinstance(repo_full_name, str)
        and GITHUB_REPOSITORY_PATTERN.fullmatch(repo_full_name)
        and pr_number > 0
        and isinstance(pr_url, str)
        and pr_url.startswith(f"{scm_base_url}/")
        and isinstance(head_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(head_sha)
        and isinstance(base_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(base_sha)
        and isinstance(updated_at, str)
        and isinstance(author, str)
        and isinstance(head_branch, str)
        and isinstance(base_branch, str)
        and isinstance(is_draft, bool)
        and isinstance(title, str)
        and isinstance(description, str)
        and isinstance(changed_file_count, int)
        and not isinstance(changed_file_count, bool)
        and 0 <= changed_file_count <= 1_000_000
        and state in {"open", "closed", "merged"}
        and isinstance(source_created_at, str)
        and isinstance(source_closed_at, str)
        and isinstance(source_merged_at, str)
        and isinstance(additions, int)
        and not isinstance(additions, bool)
        and 0 <= additions <= 100_000_000
        and isinstance(deletions, int)
        and not isinstance(deletions, bool)
        and 0 <= deletions <= 100_000_000
        and isinstance(raw_labels, list)
        and all(isinstance(label, str) for label in labels)
        and bool(delivery_id.strip())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request payload",
        )

    try:
        return PullRequestEvent.from_payload(
            {
                "provider": "github",
                "scm_base_url": scm_base_url,
                "api_base_url": api_base_url,
                "repo_full_name": repo_full_name,
                "number": pr_number,
                "web_url": pr_url,
                "action": action,
                "head_sha": head_sha.lower(),
                "base_sha": base_sha.lower(),
                "updated_at": updated_at,
                "delivery_id": delivery_id,
                "author": author,
                "base_branch": base_branch,
                "head_branch": head_branch,
                "is_draft": is_draft,
                "labels": labels,
                "title": title,
                "description": description,
                "trigger_kind": trigger_kind,
                "trigger_id": trigger_id,
                "metadata_complete": True,
                "changed_file_count": changed_file_count,
                "state": state,
                "source_created_at": source_created_at,
                "source_closed_at": source_closed_at,
                "source_merged_at": source_merged_at,
                "additions": additions,
                "deletions": deletions,
            }
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request payload",
        ) from error


def normalize_manual_review_request(payload: dict) -> ManualReviewRequest | None:
    try:
        repo_full_name = payload["repository"]["full_name"]
        issue = payload["issue"]
        number = int(issue["number"])
        is_pull_request = isinstance(issue.get("pull_request"), dict)
        comment = payload["comment"]
        comment_id = str(comment["id"])
        body = comment["body"]
        requested_at = comment["created_at"]
        requested_by = comment["user"]["login"]
        author_association = comment["author_association"]
        actor_type = comment["user"]["type"]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed issue_comment payload",
        ) from error
    if not is_pull_request:
        return None

    valid = (
        isinstance(repo_full_name, str)
        and GITHUB_REPOSITORY_PATTERN.fullmatch(repo_full_name)
        and number > 0
        and 0 < len(comment_id) <= 255
        and isinstance(body, str)
        and len(body) <= 65_536
        and isinstance(requested_at, str)
        and isinstance(requested_by, str)
        and 0 < len(requested_by) <= 255
        and isinstance(author_association, str)
        and isinstance(actor_type, str)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed issue_comment payload",
        )
    if (
        actor_type.casefold() == "bot"
        or author_association not in MANUAL_TRIGGER_ASSOCIATIONS
        or not is_manual_review_trigger(body)
    ):
        return None
    return ManualReviewRequest(
        repo_full_name=repo_full_name,
        number=number,
        trigger_id=f"issue-comment:{comment_id}",
        requested_by=requested_by,
        requested_at=requested_at,
    )


def normalize_review_conversation_event(
    payload: dict,
    *,
    delivery_id: str,
) -> ReviewConversationEvent | None:
    try:
        repo_full_name = payload["repository"]["full_name"]
        pull_request = payload["pull_request"]
        number = int(pull_request["number"])
        state = pull_request["state"]
        head_sha = pull_request["head"]["sha"]
        base_sha = pull_request["base"]["sha"]
        comment = payload["comment"]
        external_comment_id = str(comment["id"])
        root_value = comment.get("in_reply_to_id")
        body = comment["body"]
        created_at = comment["created_at"]
        author = comment["user"]["login"]
        actor_type = comment["user"]["type"]
        author_association = comment["author_association"]
        file_path = comment["path"]
        line_value = comment.get("line") or comment.get("original_line")
        side_value = comment.get("side") or comment.get("original_side")
        diff_hunk = comment.get("diff_hunk") or ""
        comment_commit_sha = comment["commit_id"]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        ) from error

    valid = (
        isinstance(repo_full_name, str)
        and GITHUB_REPOSITORY_PATTERN.fullmatch(repo_full_name)
        and number > 0
        and state == "open"
        and isinstance(head_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(head_sha)
        and isinstance(base_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(base_sha)
        and external_comment_id.isdigit()
        and isinstance(body, str)
        and len(body) <= 65_536
        and isinstance(created_at, str)
        and isinstance(author, str)
        and 0 < len(author) <= 255
        and isinstance(actor_type, str)
        and isinstance(author_association, str)
        and isinstance(file_path, str)
        and isinstance(line_value, int)
        and not isinstance(line_value, bool)
        and line_value > 0
        and isinstance(side_value, str)
        and isinstance(diff_hunk, str)
        and len(diff_hunk) <= 100_000
        and isinstance(comment_commit_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(comment_commit_sha)
        and bool(delivery_id.strip())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        )
    if (
        root_value is None
        or not str(root_value).isdigit()
        or actor_type.casefold() == "bot"
        or author_association not in MANUAL_TRIGGER_ASSOCIATIONS
        or is_diffuse_generated(body)
    ):
        return None
    question = conversation_question(body)
    if question is None:
        return None

    try:
        return ReviewConversationEvent(
            provider="github",
            scm_base_url=os.environ.get("GITHUB_WEB_URL", "https://github.com"),
            api_base_url=os.environ.get(
                "GITHUB_API_URL",
                "https://api.github.com",
            ),
            repo_full_name=repo_full_name,
            number=number,
            delivery_id=delivery_id,
            external_comment_id=external_comment_id,
            root_comment_id=str(root_value),
            head_sha=head_sha,
            base_sha=base_sha,
            comment_commit_sha=comment_commit_sha,
            author=author,
            author_association=author_association,
            created_at=created_at,
            question=question,
            file_path=file_path,
            line=line_value,
            side=side_value,
            diff_hunk=diff_hunk,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        ) from error


def normalize_review_feedback_comment_event(
    payload: dict,
    *,
    delivery_id: str,
) -> ReviewFeedbackCommentEvent | None:
    try:
        repo_full_name = payload["repository"]["full_name"]
        pull_request = payload["pull_request"]
        number = int(pull_request["number"])
        state = pull_request["state"]
        comment = payload["comment"]
        external_comment_id = str(comment["id"])
        root_value = comment.get("in_reply_to_id")
        body = comment["body"]
        created_at = comment["created_at"]
        author = comment["user"]["login"]
        actor_type = comment["user"]["type"]
        author_association = comment["author_association"]
        file_path = comment["path"]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        ) from error

    valid = (
        isinstance(repo_full_name, str)
        and GITHUB_REPOSITORY_PATTERN.fullmatch(repo_full_name)
        and number > 0
        and state == "open"
        and external_comment_id.isdigit()
        and isinstance(body, str)
        and 0 < len(body.strip()) <= 65_536
        and isinstance(created_at, str)
        and isinstance(author, str)
        and 0 < len(author) <= 255
        and isinstance(actor_type, str)
        and isinstance(author_association, str)
        and isinstance(file_path, str)
        and bool(delivery_id.strip())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        )
    if (
        root_value is None
        or not str(root_value).isdigit()
        or actor_type.casefold() == "bot"
        or author_association not in MANUAL_TRIGGER_ASSOCIATIONS
        or is_human_only_discussion(body)
        or is_diffuse_generated(body)
    ):
        return None

    try:
        return ReviewFeedbackCommentEvent(
            provider="github",
            scm_base_url=os.environ.get("GITHUB_WEB_URL", "https://github.com"),
            api_base_url=os.environ.get(
                "GITHUB_API_URL",
                "https://api.github.com",
            ),
            repo_full_name=repo_full_name,
            number=number,
            delivery_id=delivery_id,
            external_comment_id=external_comment_id,
            root_comment_id=str(root_value),
            author=author,
            author_association=author_association,
            created_at=created_at,
            body=body,
            file_path=file_path,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pull_request_review_comment payload",
        ) from error


def _github_json_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-manual-review",
    }
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_manual_pull_request_event(
    request: ManualReviewRequest,
    *,
    delivery_id: str,
    client: httpx.AsyncClient | None = None,
    scm_base_url: str | None = None,
    api_base_url: str | None = None,
) -> PullRequestEvent:
    normalized_scm_base_url = normalize_base_url(
        scm_base_url or os.environ.get("GITHUB_WEB_URL", "https://github.com"),
        field_name="GITHUB_WEB_URL",
    )
    normalized_api_base_url = normalize_base_url(
        api_base_url or os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        field_name="GITHUB_API_URL",
    )
    owner, repository = request.repo_full_name.split("/", maxsplit=1)
    url = (
        f"{normalized_api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{request.number}"
    )

    async def fetch(active_client: httpx.AsyncClient) -> dict:
        response = await active_client.get(url, headers=_github_json_headers())
        raise_for_provider_status(response, provider="github")
        if len(response.content) > MAX_METADATA_BYTES:
            raise RuntimeError("Pull-request metadata exceeds Diffuse's size limit")
        value = response.json()
        if not isinstance(value, dict) or value.get("state") != "open":
            raise ValueError("Manual reviews require an open pull request")
        return value

    if client is None:
        async with httpx.AsyncClient(timeout=scm_api_timeout_seconds()) as owned_client:
            pull_request = await fetch(owned_client)
    else:
        pull_request = await fetch(client)
    return normalize_pull_request_event(
        {
            "repository": {"full_name": request.repo_full_name},
            "pull_request": pull_request,
        },
        delivery_id=delivery_id,
        action="manual",
        trigger_kind="manual",
        trigger_id=request.trigger_id,
        updated_at_override=request.requested_at,
        scm_base_url_override=normalized_scm_base_url,
        api_base_url_override=normalized_api_base_url,
    )


def normalize_push_event(
    payload: dict,
    *,
    delivery_id: str,
) -> PushEvent | None:
    try:
        repository = payload["repository"]
        repo_full_name = repository["full_name"]
        default_branch = repository["default_branch"]
        pushed_at = int(repository["pushed_at"])
        ref_name = payload["ref"]
        before_sha = payload["before"]
        after_sha = payload["after"]
        deleted = bool(payload["deleted"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed push payload",
        ) from error

    valid = (
        isinstance(repo_full_name, str)
        and GITHUB_REPOSITORY_PATTERN.fullmatch(repo_full_name)
        and isinstance(default_branch, str)
        and isinstance(ref_name, str)
        and isinstance(before_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(before_sha)
        and isinstance(after_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(after_sha)
        and bool(delivery_id.strip())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed push payload",
        )
    if deleted or ref_name != f"refs/heads/{default_branch}":
        return None

    try:
        pushed_at_iso = datetime.fromtimestamp(pushed_at, UTC).isoformat()
        return PushEvent.from_payload(
            {
                "provider": "github",
                "scm_base_url": os.environ.get("GITHUB_WEB_URL", "https://github.com"),
                "api_base_url": os.environ.get(
                    "GITHUB_API_URL",
                    "https://api.github.com",
                ),
                "repo_full_name": repo_full_name,
                "ref_name": ref_name,
                "default_branch": default_branch,
                "before_sha": before_sha,
                "after_sha": after_sha,
                "pushed_at": pushed_at_iso,
                "delivery_id": delivery_id,
            }
        )
    except (OSError, OverflowError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed push payload",
        ) from error


async def _fetch_diff_url(
    url: str,
    *,
    user_agent: str,
    client: httpx.AsyncClient | None = None,
) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github.diff",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async def fetch(active_client: httpx.AsyncClient) -> str:
        content = bytearray()
        async with active_client.stream("GET", url, headers=headers) as response:
            raise_for_provider_status(response, provider="github")
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_DIFF_BYTES:
                    raise RuntimeError(
                        "SCM diff exceeds Diffuse's configured size limit"
                    )
        return bytes(content).decode("utf-8", errors="replace")

    if client is not None:
        return await fetch(client)
    async with httpx.AsyncClient(timeout=scm_api_timeout_seconds()) as owned_client:
        return await fetch(owned_client)


async def fetch_pull_request_diff(event: PullRequestEvent) -> str:
    if event.provider != "github":
        raise ValueError(f"Unsupported SCM provider: {event.provider}")
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    url = (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{event.number}"
    )
    return await _fetch_diff_url(url, user_agent="diffuse-context-review")


async def fetch_pull_request_update_diff(
    event: PullRequestEvent,
    previous_head_sha: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch only the commits added since the previously published review."""
    if event.provider != "github":
        raise ValueError(f"Unsupported SCM provider: {event.provider}")
    previous_head_sha = previous_head_sha.strip().lower()
    if not COMMIT_SHA_PATTERN.fullmatch(previous_head_sha):
        raise ValueError("Previous review head must be a full commit digest")
    if previous_head_sha == event.head_sha:
        return ""
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    comparison = (
        f"{quote(previous_head_sha, safe='')}..."
        f"{quote(event.head_sha, safe='')}"
    )
    url = (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/compare/{comparison}"
    )
    return await _fetch_diff_url(
        url,
        user_agent="diffuse-finding-continuity",
        client=client,
    )
