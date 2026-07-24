"""GitLab webhook authentication, normalization, and metadata enrichment."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status

from service.review_description import is_managed_review_description_change
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
    normalize_timestamp,
    validate_repository_name,
)

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
STANDARD_SIGNATURE_PATTERN = re.compile(r"^v1,([A-Za-z0-9+/]+={0,2})$")
MAX_METADATA_BYTES = 1_000_000
ZERO_SHA_PATTERN = re.compile(r"^0{40,64}$")


@dataclass(frozen=True)
class VerifiedGitLabWebhook:
    delivery_id: str
    scm_base_url: str
    api_base_url: str
    authentication: str


class GitLabMetadataPendingError(RuntimeError):
    """GitLab has accepted an MR but has not prepared its diff identity yet."""


@dataclass(frozen=True)
class GitLabReviewInteraction:
    feedback: ReviewFeedbackCommentEvent | None
    conversation: ReviewConversationEvent | None
    manual_review: PullRequestEvent | None = None
    manual_requested_by: str | None = None


def gitlab_merge_request_action(payload: dict) -> str | None:
    try:
        attributes = payload["object_attributes"]
        action = attributes["action"]
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request payload",
        ) from error
    if action == "open":
        return "opened"
    if action == "close":
        return "closed"
    if action == "reopen":
        return "reopened"
    if action == "merge":
        return "closed"
    if action != "update":
        return None
    if isinstance(attributes.get("oldrev"), str):
        return "synchronize"
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return None
    review_relevant = {
        "description",
        "draft",
        "labels",
        "state_id",
        "target_branch",
        "title",
    }
    relevant_changes = review_relevant.intersection(changes)
    if relevant_changes == {"description"}:
        description_change = changes.get("description")
        if isinstance(description_change, dict):
            current = description_change.get(
                "current",
                attributes.get("description"),
            )
            if is_managed_review_description_change(
                description_change.get("previous"),
                current,
            ):
                return None
    return "edited" if relevant_changes else None


def _configuration_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


def _authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid GitLab webhook authentication",
    )


def _normalized_configured_url(value: str, *, field_name: str) -> str:
    try:
        return normalize_base_url(value, field_name=field_name)
    except ValueError as error:
        raise _configuration_error(f"{field_name} is invalid") from error


def resolve_gitlab_instance(instance_header: str) -> tuple[str, str]:
    primary = _normalized_configured_url(
        os.environ.get("GITLAB_WEB_URL", "https://gitlab.com"),
        field_name="GITLAB_WEB_URL",
    )
    allowed = {primary}
    for raw_value in os.environ.get("GITLAB_ALLOWED_INSTANCES", "").split(","):
        if not raw_value.strip():
            continue
        allowed.add(
            _normalized_configured_url(
                raw_value.strip(),
                field_name="GITLAB_ALLOWED_INSTANCES",
            )
        )
    if len(allowed) > 16:
        raise _configuration_error("GITLAB_ALLOWED_INSTANCES exceeds 16 hosts")

    if instance_header:
        try:
            instance = normalize_base_url(
                instance_header,
                field_name="X-Gitlab-Instance",
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Gitlab-Instance is invalid",
            ) from error
    else:
        instance = primary
    if instance not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="GitLab instance is not allowlisted",
        )

    if instance == primary:
        api_base_url = _normalized_configured_url(
            os.environ.get("GITLAB_API_URL", f"{primary}/api/v4"),
            field_name="GITLAB_API_URL",
        )
    else:
        api_base_url = f"{instance}/api/v4"
    return instance, api_base_url


def _standard_signing_key(token: str) -> bytes:
    if not token.startswith("whsec_"):
        raise _configuration_error(
            "GITLAB_WEBHOOK_SIGNING_TOKEN must be a Standard Webhooks whsec_ token"
        )
    encoded = token.removeprefix("whsec_")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _configuration_error(
            "GITLAB_WEBHOOK_SIGNING_TOKEN is not valid base64"
        ) from error
    if len(key) < 16:
        raise _configuration_error(
            "GITLAB_WEBHOOK_SIGNING_TOKEN decodes to an undersized key"
        )
    return key


def _verify_standard_signature(
    body: bytes,
    *,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    now: datetime,
) -> None:
    token = os.environ.get("GITLAB_WEBHOOK_SIGNING_TOKEN", "")
    if not token:
        raise _configuration_error(
            "GITLAB_WEBHOOK_SIGNING_TOKEN is required for signed GitLab webhooks"
        )
    if not (0 < len(webhook_id) <= 255 and webhook_timestamp.isdigit()):
        raise _authentication_error()
    try:
        timestamp_value = int(webhook_timestamp)
        signed_at = datetime.fromtimestamp(timestamp_value, UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise _authentication_error() from error
    try:
        max_age = int(os.environ.get("GITLAB_WEBHOOK_MAX_AGE_SECONDS", "300"))
    except ValueError as error:
        raise _configuration_error(
            "GITLAB_WEBHOOK_MAX_AGE_SECONDS must be an integer"
        ) from error
    if not 30 <= max_age <= 3600:
        raise _configuration_error(
            "GITLAB_WEBHOOK_MAX_AGE_SECONDS must be between 30 and 3600"
        )
    if abs((now.astimezone(UTC) - signed_at).total_seconds()) > max_age:
        raise _authentication_error()

    key = _standard_signing_key(token)
    signed = webhook_id.encode() + b"." + webhook_timestamp.encode() + b"." + body
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    candidates: list[bytes] = []
    for value in webhook_signature.split():
        match = STANDARD_SIGNATURE_PATTERN.fullmatch(value)
        if not match:
            continue
        try:
            candidates.append(base64.b64decode(match.group(1), validate=True))
        except (binascii.Error, ValueError):
            continue
    if not candidates or not any(
        hmac.compare_digest(expected, candidate) for candidate in candidates
    ):
        raise _authentication_error()


def verify_gitlab_webhook(
    body: bytes,
    *,
    webhook_id: str = "",
    webhook_timestamp: str = "",
    webhook_signature: str = "",
    legacy_token: str = "",
    idempotency_key: str = "",
    event_uuid: str = "",
    instance_header: str = "",
    now: datetime | None = None,
) -> VerifiedGitLabWebhook:
    standard_headers_present = any(
        (webhook_id, webhook_timestamp, webhook_signature)
    )
    if standard_headers_present:
        if not all((webhook_id, webhook_timestamp, webhook_signature)):
            raise _authentication_error()
        _verify_standard_signature(
            body,
            webhook_id=webhook_id,
            webhook_timestamp=webhook_timestamp,
            webhook_signature=webhook_signature,
            now=now or datetime.now(UTC),
        )
        delivery_id = webhook_id
        authentication = "standard_webhooks"
    else:
        secret = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
        if not secret:
            raise _configuration_error(
                "GitLab webhook authentication is not configured"
            )
        if not legacy_token or not hmac.compare_digest(secret, legacy_token):
            raise _authentication_error()
        delivery_id = idempotency_key or event_uuid
        authentication = "legacy_token"

    if not (0 < len(delivery_id) <= 255):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitLab webhook is missing a stable delivery identifier",
        )
    scm_base_url, api_base_url = resolve_gitlab_instance(instance_header)
    return VerifiedGitLabWebhook(
        delivery_id=delivery_id,
        scm_base_url=scm_base_url,
        api_base_url=api_base_url,
        authentication=authentication,
    )


def _headers(*, user_agent: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


def _gitlab_timestamp(value: str) -> str:
    if value.endswith(" UTC"):
        value = f"{value[:-4]}+00:00"
    return normalize_timestamp(value)


def _required_webhook_project(
    payload: dict,
    verified: VerifiedGitLabWebhook,
) -> tuple[int, str]:
    try:
        project = payload["project"]
        project_id = project["id"]
        repo_full_name = project["path_with_namespace"]
        web_url = project["web_url"]
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab project payload",
        ) from error
    valid = (
        isinstance(project_id, int)
        and not isinstance(project_id, bool)
        and project_id > 0
        and isinstance(repo_full_name, str)
        and isinstance(web_url, str)
        and web_url == f"{verified.scm_base_url}/{repo_full_name}"
    )
    try:
        validate_repository_name(repo_full_name)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab project payload",
        )
    return project_id, repo_full_name


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    user_agent: str,
) -> object:
    response = await client.get(url, headers=_headers(user_agent=user_agent))
    response.raise_for_status()
    if len(response.content) > MAX_METADATA_BYTES:
        raise RuntimeError("GitLab metadata exceeds Diffuse's size limit")
    return response.json()


def _diff_refs(
    merge_request: dict,
    versions: object | None,
) -> tuple[str, str, str]:
    current_head = merge_request.get("sha")
    if (
        not isinstance(current_head, str)
        or not COMMIT_SHA_PATTERN.fullmatch(current_head)
    ):
        raise GitLabMetadataPendingError(
            "GitLab has not prepared the merge-request head revision"
        )
    refs = merge_request.get("diff_refs")
    base_sha = refs.get("base_sha") if isinstance(refs, dict) else None
    head_sha = refs.get("head_sha") if isinstance(refs, dict) else None
    start_sha = refs.get("start_sha") if isinstance(refs, dict) else None
    if (
        isinstance(base_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(base_sha)
        and isinstance(head_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(head_sha)
        and isinstance(start_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(start_sha)
        and head_sha.casefold() == current_head.casefold()
    ):
        return base_sha, head_sha, start_sha

    if not isinstance(versions, list):
        raise GitLabMetadataPendingError(
            "GitLab has not prepared merge-request diff references"
        )
    for version in versions:
        if not isinstance(version, dict):
            continue
        candidate_head = version.get("head_commit_sha")
        candidate_base = version.get("base_commit_sha")
        candidate_start = version.get("start_commit_sha")
        if (
            isinstance(candidate_head, str)
            and COMMIT_SHA_PATTERN.fullmatch(candidate_head)
            and isinstance(candidate_base, str)
            and COMMIT_SHA_PATTERN.fullmatch(candidate_base)
            and isinstance(candidate_start, str)
            and COMMIT_SHA_PATTERN.fullmatch(candidate_start)
            and candidate_head.casefold() == current_head.casefold()
        ):
            return candidate_base, candidate_head, candidate_start
    raise GitLabMetadataPendingError(
        "GitLab has not prepared merge-request diff references"
    )


def _changed_file_count(
    merge_request: dict,
    versions: object | None,
) -> tuple[int, bool]:
    raw_count = merge_request.get("changes_count")
    if isinstance(raw_count, str) and raw_count.isdigit():
        return int(raw_count), True
    if isinstance(versions, list):
        for version in versions:
            if not isinstance(version, dict):
                continue
            real_size = version.get("real_size")
            if isinstance(real_size, str) and real_size.isdigit():
                return int(real_size), True
    if raw_count == "1000+":
        return 1000, False
    raise GitLabMetadataPendingError(
        "GitLab has not prepared merge-request change metadata"
    )


async def fetch_gitlab_merge_request_event(
    payload: dict,
    *,
    verified: VerifiedGitLabWebhook,
    action: str,
    trigger_kind: str = "automatic",
    trigger_id: str = "",
    updated_at_override: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> PullRequestEvent:
    project_id, repo_full_name = _required_webhook_project(payload, verified)
    try:
        attributes = payload["object_attributes"]
        number = attributes.get("iid")
        if number is None:
            number = payload["merge_request"]["iid"]
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request payload",
        ) from error
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request payload",
        )
    project_path = quote(str(project_id), safe="")
    mr_path = (
        f"{verified.api_base_url}/projects/{project_path}/"
        f"merge_requests/{number}"
    )

    async def fetch(active_client: httpx.AsyncClient) -> tuple[dict, object | None]:
        value = await _get_json(
            active_client,
            mr_path,
            user_agent="diffuse-gitlab-webhook",
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitLab returned invalid merge-request metadata")
        refs = value.get("diff_refs")
        current_head = value.get("sha")
        versions: object | None = None
        if not (
            isinstance(refs, dict)
            and isinstance(refs.get("base_sha"), str)
            and isinstance(refs.get("head_sha"), str)
            and isinstance(refs.get("start_sha"), str)
            and isinstance(current_head, str)
            and refs.get("head_sha").casefold() == current_head.casefold()
        ) or value.get("changes_count") in {None, "1000+"}:
            versions = await _get_json(
                active_client,
                f"{mr_path}/versions",
                user_agent="diffuse-gitlab-webhook",
            )
        return value, versions

    if client is None:
        timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            merge_request, versions = await fetch(owned_client)
    else:
        merge_request, versions = await fetch(client)

    base_sha, head_sha, start_sha = _diff_refs(merge_request, versions)
    changed_file_count, metadata_complete = _changed_file_count(
        merge_request,
        versions,
    )
    try:
        response_project_id = merge_request["project_id"]
        source_project_id = merge_request["source_project_id"]
        response_number = merge_request["iid"]
        web_url = merge_request["web_url"]
        author = merge_request["author"]["username"]
        base_branch = merge_request["target_branch"]
        head_branch = merge_request["source_branch"]
        is_draft = merge_request["draft"]
        raw_labels = merge_request["labels"]
        title = merge_request["title"]
        description = merge_request.get("description") or ""
        raw_state = merge_request["state"]
        created_at = merge_request["created_at"]
        updated_at = merge_request["updated_at"]
        closed_at = merge_request.get("closed_at") or ""
        merged_at = merge_request.get("merged_at") or ""
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request metadata",
        ) from error
    state_map = {
        "opened": "open",
        "locked": "open",
        "closed": "closed",
        "merged": "merged",
    }
    state = state_map.get(raw_state)
    labels = (
        tuple(
            label.get("name") if isinstance(label, dict) else label
            for label in raw_labels
        )
        if isinstance(raw_labels, list)
        else ()
    )
    valid = (
        response_project_id == project_id
        and isinstance(source_project_id, int)
        and not isinstance(source_project_id, bool)
        and source_project_id > 0
        and response_number == number
        and isinstance(web_url, str)
        and web_url.startswith(f"{verified.scm_base_url}/{repo_full_name}/")
        and isinstance(author, str)
        and isinstance(base_branch, str)
        and isinstance(head_branch, str)
        and isinstance(is_draft, bool)
        and isinstance(raw_labels, list)
        and all(isinstance(label, str) for label in labels)
        and isinstance(title, str)
        and isinstance(description, str)
        and state is not None
        and isinstance(created_at, str)
        and isinstance(updated_at, str)
        and isinstance(closed_at, str)
        and isinstance(merged_at, str)
        and changed_file_count <= 1_000_000
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request metadata",
        )
    if state == "open":
        closed_at = ""
        merged_at = ""
    elif state == "closed":
        merged_at = ""

    try:
        return PullRequestEvent.from_payload(
            {
                "provider": "gitlab",
                "scm_base_url": verified.scm_base_url,
                "api_base_url": verified.api_base_url,
                "repo_full_name": repo_full_name,
                "number": number,
                "web_url": web_url,
                "action": action,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "updated_at": (
                    _gitlab_timestamp(updated_at_override)
                    if updated_at_override is not None
                    else _gitlab_timestamp(updated_at)
                ),
                "delivery_id": verified.delivery_id,
                "author": author,
                "base_branch": base_branch,
                "head_branch": head_branch,
                "is_draft": is_draft,
                "labels": labels,
                "title": title,
                "description": description,
                "trigger_kind": trigger_kind,
                "trigger_id": trigger_id,
                "metadata_complete": metadata_complete,
                "changed_file_count": changed_file_count,
                "state": state,
                "source_created_at": _gitlab_timestamp(created_at),
                "source_closed_at": (
                    _gitlab_timestamp(closed_at) if closed_at else ""
                ),
                "source_merged_at": (
                    _gitlab_timestamp(merged_at) if merged_at else ""
                ),
                "additions": 0,
                "deletions": 0,
                "source_project_id": source_project_id,
                "start_sha": start_sha,
            }
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab merge-request metadata",
        ) from error


async def fetch_manual_gitlab_merge_request_event(
    request: ManualReviewRequest,
    *,
    delivery_id: str,
    client: httpx.AsyncClient | None = None,
    scm_base_url: str | None = None,
    api_base_url: str | None = None,
) -> PullRequestEvent:
    """Fetch an authorized MCP target's current GitLab project and MR state."""
    normalized_scm_base_url = normalize_base_url(
        scm_base_url or os.environ.get("GITLAB_WEB_URL", "https://gitlab.com"),
        field_name="GITLAB_WEB_URL",
    )
    normalized_api_base_url = normalize_base_url(
        api_base_url
        or os.environ.get("GITLAB_API_URL", f"{normalized_scm_base_url}/api/v4"),
        field_name="GITLAB_API_URL",
    )
    repo_full_name = validate_repository_name(request.repo_full_name)
    if (
        not isinstance(request.number, int)
        or isinstance(request.number, bool)
        or request.number <= 0
    ):
        raise ValueError("Merge-request number must be positive")
    project_url = (
        f"{normalized_api_base_url}/projects/"
        f"{quote(repo_full_name, safe='')}"
    )

    async def fetch(active_client: httpx.AsyncClient) -> PullRequestEvent:
        project = await _get_json(
            active_client,
            project_url,
            user_agent="diffuse-gitlab-manual-review",
        )
        if not isinstance(project, dict):
            raise RuntimeError("GitLab returned invalid project metadata")
        project_id = project.get("id")
        valid_project = (
            isinstance(project_id, int)
            and not isinstance(project_id, bool)
            and project_id > 0
            and project.get("path_with_namespace") == repo_full_name
            and project.get("web_url")
            == f"{normalized_scm_base_url}/{repo_full_name}"
        )
        if not valid_project:
            raise ValueError("GitLab project identity does not match the MCP target")
        return await fetch_gitlab_merge_request_event(
            {
                "project": {
                    "id": project_id,
                    "path_with_namespace": repo_full_name,
                    "web_url": f"{normalized_scm_base_url}/{repo_full_name}",
                },
                "object_attributes": {"iid": request.number},
            },
            verified=VerifiedGitLabWebhook(
                delivery_id=delivery_id,
                scm_base_url=normalized_scm_base_url,
                api_base_url=normalized_api_base_url,
                authentication="mcp",
            ),
            action="manual",
            trigger_kind="manual",
            trigger_id=request.trigger_id,
            updated_at_override=request.requested_at,
            client=active_client,
        )

    if client is None:
        timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            event = await fetch(owned_client)
    else:
        event = await fetch(client)
    if event.state != "open":
        raise ValueError("Manual reviews require an open merge request")
    return event


async def _gitlab_author_association(
    client: httpx.AsyncClient,
    *,
    verified: VerifiedGitLabWebhook,
    project_id: int,
    user_id: int,
) -> str | None:
    response = await client.get(
        (
            f"{verified.api_base_url}/projects/{quote(str(project_id), safe='')}/"
            f"members/all/{user_id}"
        ),
        headers=_headers(user_agent="diffuse-gitlab-review-interaction"),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if len(response.content) > MAX_METADATA_BYTES:
        raise RuntimeError("GitLab member metadata exceeds Diffuse's size limit")
    value = response.json()
    access_level = value.get("access_level") if isinstance(value, dict) else None
    if not isinstance(access_level, int) or isinstance(access_level, bool):
        raise RuntimeError("GitLab returned invalid member metadata")
    if access_level >= 50:
        return "OWNER"
    if access_level >= 40:
        return "MEMBER"
    if access_level >= 30:
        return "COLLABORATOR"
    return None


async def _find_gitlab_note_discussion(
    client: httpx.AsyncClient,
    *,
    merge_request_path: str,
    note_id: int,
) -> tuple[str, list[dict]]:
    for page in range(1, 21):
        response = await client.get(
            f"{merge_request_path}/discussions",
            headers=_headers(user_agent="diffuse-gitlab-review-interaction"),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        if len(response.content) > MAX_METADATA_BYTES:
            raise RuntimeError("GitLab discussion metadata exceeds Diffuse's size limit")
        discussions = response.json()
        if not isinstance(discussions, list):
            raise RuntimeError("GitLab returned invalid discussion metadata")
        for discussion in discussions:
            if not isinstance(discussion, dict):
                raise RuntimeError("GitLab returned invalid discussion metadata")
            discussion_id = discussion.get("id")
            notes = discussion.get("notes")
            if (
                not isinstance(discussion_id, str)
                or not discussion_id
                or not isinstance(notes, list)
                or any(not isinstance(note, dict) for note in notes)
            ):
                raise RuntimeError("GitLab returned invalid discussion metadata")
            if any(note.get("id") == note_id for note in notes):
                return discussion_id, notes
        if len(discussions) < 100:
            break
    raise GitLabMetadataPendingError(
        "GitLab has not made the merge-request note discussion visible"
    )


async def fetch_gitlab_review_interaction(
    payload: dict,
    *,
    verified: VerifiedGitLabWebhook,
    client: httpx.AsyncClient | None = None,
) -> GitLabReviewInteraction:
    project_id, repo_full_name = _required_webhook_project(payload, verified)
    try:
        attributes = payload["object_attributes"]
        note_id = attributes["id"]
        noteable_type = attributes["noteable_type"]
        action = attributes["action"]
        body = attributes["note"]
        created_at = attributes["created_at"]
        system = attributes["system"]
        internal = attributes.get("internal", False)
        user = payload["user"]
        user_id = user["id"]
        username = user["username"]
        merge_request = payload["merge_request"]
        number = merge_request["iid"]
        state = merge_request["state"]
        target_project_id = merge_request["target_project_id"]
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab note payload",
        ) from error
    valid = (
        isinstance(note_id, int)
        and not isinstance(note_id, bool)
        and note_id > 0
        and noteable_type == "MergeRequest"
        and action == "create"
        and isinstance(body, str)
        and 0 < len(body.strip()) <= 65_536
        and isinstance(created_at, str)
        and system is False
        and isinstance(internal, bool)
        and isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and user_id > 0
        and isinstance(username, str)
        and 0 < len(username) <= 255
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and state == "opened"
        and target_project_id == project_id
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab note payload",
        )
    if is_diffuse_generated(body):
        return GitLabReviewInteraction(feedback=None, conversation=None)

    project_path = quote(str(project_id), safe="")
    merge_request_path = (
        f"{verified.api_base_url}/projects/{project_path}/"
        f"merge_requests/{number}"
    )

    async def fetch(active_client: httpx.AsyncClient) -> GitLabReviewInteraction:
        association = await _gitlab_author_association(
            active_client,
            verified=verified,
            project_id=project_id,
            user_id=user_id,
        )
        if association is None:
            return GitLabReviewInteraction(feedback=None, conversation=None)
        metadata = await _get_json(
            active_client,
            merge_request_path,
            user_agent="diffuse-gitlab-review-interaction",
        )
        if not isinstance(metadata, dict):
            raise RuntimeError("GitLab returned invalid merge-request metadata")
        refs = metadata.get("diff_refs")
        versions: object | None = None
        if not (
            isinstance(refs, dict)
            and isinstance(refs.get("base_sha"), str)
            and isinstance(refs.get("head_sha"), str)
            and isinstance(refs.get("start_sha"), str)
            and refs.get("head_sha") == metadata.get("sha")
        ):
            versions = await _get_json(
                active_client,
                f"{merge_request_path}/versions",
                user_agent="diffuse-gitlab-review-interaction",
            )
        base_sha, head_sha, _start_sha = _diff_refs(metadata, versions)
        if (
            metadata.get("project_id") != project_id
            or metadata.get("iid") != number
            or metadata.get("state") != "opened"
        ):
            raise RuntimeError("GitLab note no longer maps to an open merge request")

        discussion_id, notes = await _find_gitlab_note_discussion(
            active_client,
            merge_request_path=merge_request_path,
            note_id=note_id,
        )
        note_index = next(
            (index for index, note in enumerate(notes) if note.get("id") == note_id),
            -1,
        )
        if note_index == 0:
            if notes[0].get("position") is not None:
                return GitLabReviewInteraction(feedback=None, conversation=None)
            if not is_manual_review_trigger(body):
                return GitLabReviewInteraction(feedback=None, conversation=None)
            manual_review = await fetch_gitlab_merge_request_event(
                payload,
                verified=verified,
                action="manual",
                trigger_kind="manual",
                trigger_id=f"note:{note_id}",
                updated_at_override=created_at,
                client=active_client,
            )
            if manual_review.state != "open":
                raise RuntimeError(
                    "GitLab manual review no longer maps to an open merge request"
                )
            return GitLabReviewInteraction(
                feedback=None,
                conversation=None,
                manual_review=manual_review,
                manual_requested_by=username,
            )
        if note_index < 0:
            raise GitLabMetadataPendingError(
                "GitLab has not made the merge-request note visible"
            )
        root = notes[0]
        root_id = root.get("id")
        root_body = root.get("body")
        position = root.get("position")
        if (
            not isinstance(root_id, int)
            or isinstance(root_id, bool)
            or not isinstance(root_body, str)
            or "<!-- diffuse-finding:" not in root_body
            or not isinstance(position, dict)
        ):
            return GitLabReviewInteraction(feedback=None, conversation=None)
        new_line = position.get("new_line")
        old_line = position.get("old_line")
        if isinstance(new_line, int) and not isinstance(new_line, bool) and new_line > 0:
            line = new_line
            side = "RIGHT"
            file_path = position.get("new_path")
        elif isinstance(old_line, int) and not isinstance(old_line, bool) and old_line > 0:
            line = old_line
            side = "LEFT"
            file_path = position.get("old_path")
        else:
            return GitLabReviewInteraction(feedback=None, conversation=None)
        comment_commit_sha = position.get("head_sha")
        if (
            not isinstance(file_path, str)
            or not isinstance(comment_commit_sha, str)
            or not COMMIT_SHA_PATTERN.fullmatch(comment_commit_sha)
        ):
            raise RuntimeError("GitLab returned an invalid diff-note position")

        try:
            feedback = (
                None
                if is_human_only_discussion(body)
                else ReviewFeedbackCommentEvent(
                    provider="gitlab",
                    scm_base_url=verified.scm_base_url,
                    api_base_url=verified.api_base_url,
                    repo_full_name=repo_full_name,
                    number=number,
                    delivery_id=verified.delivery_id,
                    external_comment_id=str(note_id),
                    root_comment_id=str(root_id),
                    author=username,
                    author_association=association,
                    created_at=_gitlab_timestamp(created_at),
                    body=body,
                    file_path=file_path,
                )
            )
            question = conversation_question(body)
            conversation = (
                ReviewConversationEvent(
                    provider="gitlab",
                    scm_base_url=verified.scm_base_url,
                    api_base_url=verified.api_base_url,
                    repo_full_name=repo_full_name,
                    number=number,
                    delivery_id=verified.delivery_id,
                    external_comment_id=str(note_id),
                    root_comment_id=str(root_id),
                    head_sha=head_sha,
                    base_sha=base_sha,
                    comment_commit_sha=comment_commit_sha,
                    author=username,
                    author_association=association,
                    created_at=_gitlab_timestamp(created_at),
                    question=question,
                    file_path=file_path,
                    line=line,
                    side=side,
                    diff_hunk="",
                    thread_id=discussion_id,
                )
                if question is not None
                else None
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed GitLab review interaction",
            ) from error
        return GitLabReviewInteraction(
            feedback=feedback,
            conversation=conversation,
        )

    if client is not None:
        return await fetch(client)
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await fetch(owned_client)


def normalize_gitlab_push_event(
    payload: dict,
    *,
    verified: VerifiedGitLabWebhook,
) -> PushEvent | None:
    _project_id, repo_full_name = _required_webhook_project(payload, verified)
    try:
        project = payload["project"]
        default_branch = project["default_branch"]
        ref_name = payload["ref"]
        before_sha = payload["before"]
        after_sha = payload["after"]
        pushed_at = payload.get("event_created_at")
        if not pushed_at:
            pushed_at = payload["commits"][-1]["timestamp"]
    except (IndexError, KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab push payload",
        ) from error
    valid = (
        isinstance(default_branch, str)
        and isinstance(ref_name, str)
        and isinstance(before_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(before_sha)
        and isinstance(after_sha, str)
        and COMMIT_SHA_PATTERN.fullmatch(after_sha)
        and isinstance(pushed_at, str)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab push payload",
        )
    if (
        ref_name != f"refs/heads/{default_branch}"
        or ZERO_SHA_PATTERN.fullmatch(after_sha)
    ):
        return None
    try:
        return PushEvent.from_payload(
            {
                "provider": "gitlab",
                "scm_base_url": verified.scm_base_url,
                "api_base_url": verified.api_base_url,
                "repo_full_name": repo_full_name,
                "ref_name": ref_name,
                "default_branch": default_branch,
                "before_sha": before_sha,
                "after_sha": after_sha,
                "pushed_at": _gitlab_timestamp(pushed_at),
                "delivery_id": verified.delivery_id,
            }
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed GitLab push payload",
        ) from error
