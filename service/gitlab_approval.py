"""Exact-head, remotely recoverable GitLab automatic approval."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from service.approval_publication import (
    ApprovalNotCurrentError,
    PublishedApproval,
)
from service.auto_approval import AutoApprovalDecision
from service.scm import (
    PullRequestEvent,
    scm_api_timeout_seconds,
)

MAX_APPROVAL_METADATA_BYTES = 1_000_000
PENDING_MERGE_STATUSES = frozenset({"checking", "approvals_syncing"})


def _headers() -> dict[str, str]:
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        raise RuntimeError("GITLAB_TOKEN is required to approve merge requests")
    return {
        "Accept": "application/json",
        "PRIVATE-TOKEN": token,
        "User-Agent": "diffuse-auto-approval",
    }


def _merge_request_url(event: PullRequestEvent) -> str:
    project = quote(event.repo_full_name, safe="")
    return (
        f"{event.api_base_url}/projects/{project}/"
        f"merge_requests/{event.number}"
    )


async def _json_response(
    response: httpx.Response,
    *,
    description: str,
) -> object:
    response.raise_for_status()
    if len(response.content) > MAX_APPROVAL_METADATA_BYTES:
        raise RuntimeError(f"GitLab {description} exceeds Diffuse's size limit")
    return response.json()


async def _assert_current_head_and_synced(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
) -> int:
    merge_request_url = _merge_request_url(event)
    response = await client.get(merge_request_url, headers=_headers())
    value = await _json_response(
        response,
        description="merge-request metadata",
    )
    if not isinstance(value, dict):
        raise RuntimeError("GitLab returned invalid merge-request state")
    state = value.get("state")
    is_draft = value.get("draft")
    head_sha = value.get("sha")
    detailed_status = value.get("detailed_merge_status")
    project_id = value.get("project_id")
    if (
        value.get("iid") != event.number
        or value.get("web_url") != event.web_url
        or not isinstance(project_id, int)
        or isinstance(project_id, bool)
        or project_id <= 0
    ):
        raise RuntimeError("GitLab returned mismatched merge-request identity")
    if state != "opened":
        raise ApprovalNotCurrentError(
            "pull_request_closed",
            "The merge request closed before automatic approval.",
        )
    if is_draft is not False:
        if is_draft is True:
            raise ApprovalNotCurrentError(
                "pull_request_draft",
                "The merge request became a draft before automatic approval.",
            )
        raise RuntimeError("GitLab returned invalid merge-request draft state")
    if head_sha != event.head_sha:
        raise ApprovalNotCurrentError(
            "head_changed",
            "The merge-request head changed before automatic approval.",
        )
    if not isinstance(detailed_status, str):
        raise RuntimeError("GitLab returned invalid detailed merge status")
    if detailed_status in PENDING_MERGE_STATUSES:
        raise RuntimeError(
            "GitLab has not synchronized merge-request approvals for this head"
        )

    versions_response = await client.get(
        f"{merge_request_url}/versions",
        headers=_headers(),
    )
    versions = await _json_response(
        versions_response,
        description="merge-request diff metadata",
    )
    if not isinstance(versions, list):
        raise RuntimeError("GitLab returned invalid merge-request diff metadata")
    current_version = next(
        (
            version
            for version in versions
            if isinstance(version, dict)
            and version.get("head_commit_sha") == event.head_sha
        ),
        None,
    )
    if current_version is None:
        raise RuntimeError(
            "GitLab has not prepared diff metadata for the reviewed head"
        )
    patch_id_sha = current_version.get("patch_id_sha")
    if not isinstance(patch_id_sha, str) or not patch_id_sha:
        raise RuntimeError(
            "GitLab has not synchronized the reviewed diff for approval"
        )
    return project_id


async def _authenticated_user_id(client: httpx.AsyncClient, event: PullRequestEvent) -> int:
    response = await client.get(
        f"{event.api_base_url}/user",
        headers=_headers(),
    )
    value = await _json_response(response, description="user metadata")
    user_id = value.get("id") if isinstance(value, dict) else None
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
    ):
        raise RuntimeError("GitLab returned invalid authenticated-user metadata")
    return user_id


def _has_user_approval(
    value: object,
    user_id: int,
    event: PullRequestEvent,
    project_id: int,
) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("iid") != event.number
        or value.get("project_id") != project_id
        or value.get("state") != "opened"
    ):
        raise RuntimeError("GitLab returned invalid merge-request approval state")
    approved_by = value.get("approved_by")
    if not isinstance(approved_by, list):
        raise RuntimeError("GitLab returned invalid merge-request approvers")
    for approval in approved_by:
        user = approval.get("user") if isinstance(approval, dict) else None
        if isinstance(user, dict) and user.get("id") == user_id:
            return True
    return False


def _published(event: PullRequestEvent, user_id: int) -> PublishedApproval:
    return PublishedApproval(
        external_id=(
            f"gitlab:{event.repo_full_name}:{event.number}:"
            f"{user_id}:{event.head_sha}"
        ),
        external_url=event.web_url,
    )


async def _recover_existing_approval(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    user_id: int,
    project_id: int,
) -> PublishedApproval | None:
    response = await client.get(
        f"{_merge_request_url(event)}/approvals",
        headers=_headers(),
    )
    value = await _json_response(
        response,
        description="merge-request approval state",
    )
    return _published(event, user_id) if _has_user_approval(
        value,
        user_id,
        event,
        project_id,
    ) else None


async def publish_gitlab_approval(
    event: PullRequestEvent,
    *,
    review_run_id: int,
    decision: AutoApprovalDecision,
    client: httpx.AsyncClient | None = None,
) -> PublishedApproval:
    del review_run_id
    if event.provider != "gitlab":
        raise ValueError("GitLab approval publisher received a non-GitLab event")
    if not decision.eligible:
        raise ValueError("An ineligible decision cannot publish an approval")
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _publish_with_client(owned_client, event)
    return await _publish_with_client(client, event)


async def _publish_with_client(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
) -> PublishedApproval:
    project_id = await _assert_current_head_and_synced(client, event)
    user_id = await _authenticated_user_id(client, event)
    response = await client.post(
        f"{_merge_request_url(event)}/approve",
        headers=_headers(),
        json={"sha": event.head_sha},
    )
    if response.status_code == 409:
        raise ApprovalNotCurrentError(
            "head_changed",
            "GitLab rejected automatic approval because the merge-request head changed.",
        )
    if response.status_code in {401, 403}:
        existing = await _recover_existing_approval(
            client,
            event,
            user_id,
            project_id,
        )
        if existing is not None:
            return existing
    value = await _json_response(
        response,
        description="created merge-request approval",
    )
    if not _has_user_approval(value, user_id, event, project_id):
        raise RuntimeError("GitLab did not record approval by the authenticated user")
    return _published(event, user_id)
