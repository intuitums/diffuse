"""GitLab diff access and idempotent native review publication."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from urllib.parse import quote

import httpx

from service.diff_parser import ParsedDiff, parse_unified_diff
from service.finding_lineage import ReviewContinuity
from service.finding_store import PublishedFindingComment
from service.github_review import (
    FINDING_MARKER_PATTERN,
    PublishedReview,
    _finding_comment,
    _inline_findings,
    format_review_body,
)
from service.review_description import merge_review_description
from service.review_failure_notice import (
    TerminalReviewFailure,
    format_failure_notice,
)
from service.review_models import ReviewFinding, ReviewReport
from service.review_provenance import (
    CommitMetadata,
    PullRequestCommits,
    commit_names_agent_identity,
)
from service.scm import (
    PullRequestEvent,
    scm_api_timeout_seconds,
)

MAX_DIFF_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_COMMIT_METADATA_BYTES = 2_000_000
MAX_PULL_REQUEST_COMMITS = 250
MAX_COMMIT_METADATA_PAGES = 3
MAX_COMMIT_SIGNATURE_LOOKUPS = 25
MAX_COMMIT_SIGNATURE_BYTES = 64_000
MAX_MERGE_REQUEST_DESCRIPTION_CHARS = 1_048_576
MAX_DISCUSSION_PAGES = 20
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _headers(*, accept: str = "application/json") -> dict[str, str]:
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        raise RuntimeError("GITLAB_TOKEN is required for GitLab review operations")
    return {
        "Accept": accept,
        "PRIVATE-TOKEN": token,
        "User-Agent": "diffuse-native-review",
    }


def _project_path(event: PullRequestEvent) -> str:
    return quote(event.repo_full_name, safe="")


def _source_project_path(event: PullRequestEvent) -> str:
    if event.source_project_id <= 0:
        return _project_path(event)
    return quote(str(event.source_project_id), safe="")


def _merge_request_path(event: PullRequestEvent) -> str:
    return (
        f"{event.api_base_url}/projects/{_project_path(event)}/"
        f"merge_requests/{event.number}"
    )


def _commit_url(event: PullRequestEvent) -> str:
    return (
        f"{event.scm_base_url}/{quote(event.repo_full_name, safe='/')}/"
        f"-/commit/{event.head_sha}"
    )


async def _fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, object] | None = None,
    limit: int,
) -> bytes:
    content = bytearray()
    async with client.stream("GET", url, headers=headers, params=params) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > limit:
                raise RuntimeError("GitLab response exceeds Diffuse's size limit")
    return bytes(content)


async def fetch_gitlab_merge_request_diff(
    event: PullRequestEvent,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    if event.provider != "gitlab":
        raise ValueError("GitLab diff reader received a non-GitLab event")
    url = f"{_merge_request_path(event)}/raw_diffs"

    async def fetch(active_client: httpx.AsyncClient) -> str:
        content = await _fetch_bytes(
            active_client,
            url,
            headers=_headers(accept="text/plain"),
            limit=MAX_DIFF_BYTES,
        )
        return content.decode("utf-8", errors="replace")

    if client is not None:
        return await fetch(client)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await fetch(owned_client)


def _gitlab_commit_metadata(value: object) -> CommitMetadata | None:
    if not isinstance(value, dict):
        return None
    fields = (
        value.get("id"),
        value.get("message"),
        value.get("author_name"),
        value.get("author_email"),
        value.get("committer_name"),
        value.get("committer_email"),
    )
    if not all(isinstance(field, str) for field in fields):
        return None
    try:
        return CommitMetadata(
            sha=fields[0],
            message=fields[1],
            author_name=fields[2],
            author_email=fields[3],
            committer_name=fields[4],
            committer_email=fields[5],
        )
    except ValueError:
        return None


async def _commit_signature_is_verified(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    sha: str,
) -> bool:
    """Ask GitLab whether it verified this commit's signature.

    GitLab's merge-request commits endpoint returns only Git-supplied names and
    emails, all of which the author of the change can set. This is the one
    identity assertion GitLab makes about a commit, and provenance routing needs
    it: without it an agent email on a GitLab MR stays capped below the routing
    threshold, so GitLab changes could be classified but never routed to an
    opposing model family.
    """

    url = (
        f"{event.api_base_url}/projects/{_project_path(event)}/"
        f"repository/commits/{quote(sha, safe='')}/signature"
    )
    response = await client.get(
        url,
        headers={
            **_headers(),
            "User-Agent": "diffuse-review-provenance",
        },
    )
    if response.status_code in {403, 404}:
        # Unsigned commits answer 404, and a token without repository scope
        # answers 403. Neither is an assertion, so neither raises the strength
        # of a Git-supplied identity.
        return False
    response.raise_for_status()
    if len(response.content) > MAX_COMMIT_SIGNATURE_BYTES:
        raise RuntimeError("GitLab commit signature exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("GitLab returned an invalid commit signature")
    return value.get("verification_status") == "verified"


async def _with_verified_signatures(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    commits: list[CommitMetadata],
) -> list[CommitMetadata]:
    """Attach GitLab's signature verdict to commits that name an agent identity.

    Only those commits are looked up: verification cannot turn an unrecognised
    identity into a signal, so a request per commit would buy nothing on a
    human-authored MR. The lookup budget bounds a large MR; commits beyond it
    keep the unverified default, which is conservative rather than wrong.
    """

    verified: list[CommitMetadata] = []
    lookups = 0
    for commit in commits:
        if (
            commit.verified
            or lookups >= MAX_COMMIT_SIGNATURE_LOOKUPS
            or not commit_names_agent_identity(commit)
        ):
            verified.append(commit)
            continue
        lookups += 1
        try:
            is_verified = await _commit_signature_is_verified(
                client,
                event,
                commit.sha,
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            # A signature lookup is an enrichment. Losing one leaves the commit
            # at its unverified strength rather than failing the whole review.
            verified.append(commit)
            continue
        verified.append(
            replace(commit, verified=True) if is_verified else commit
        )
    return verified


async def fetch_gitlab_merge_request_commits(
    event: PullRequestEvent,
    *,
    client: httpx.AsyncClient | None = None,
) -> PullRequestCommits:
    """Fetch bounded GitLab commit identities and trailers without source blobs."""

    if event.provider != "gitlab":
        raise ValueError("GitLab commit reader received a non-GitLab event")
    url = f"{_merge_request_path(event)}/commits"

    async def fetch(active_client: httpx.AsyncClient) -> PullRequestCommits:
        commits: list[CommitMetadata] = []
        complete = True
        page = 1
        pages_fetched = 0
        while (
            len(commits) < MAX_PULL_REQUEST_COMMITS
            and pages_fetched < MAX_COMMIT_METADATA_PAGES
        ):
            pages_fetched += 1
            response = await active_client.get(
                url,
                headers={
                    **_headers(),
                    "User-Agent": "diffuse-review-provenance",
                },
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            if len(response.content) > MAX_COMMIT_METADATA_BYTES:
                raise RuntimeError("GitLab commit metadata exceeds Diffuse's size limit")
            value = response.json()
            if not isinstance(value, list):
                raise RuntimeError("GitLab returned invalid merge-request commits")
            for item in value:
                commit = _gitlab_commit_metadata(item)
                if commit is None:
                    complete = False
                    continue
                if len(commits) == MAX_PULL_REQUEST_COMMITS:
                    complete = False
                    break
                commits.append(commit)
            next_page = response.headers.get("X-Next-Page", "").strip()
            if not next_page:
                break
            if not next_page.isdigit():
                complete = False
                break
            if len(commits) >= MAX_PULL_REQUEST_COMMITS:
                complete = False
                break
            if pages_fetched >= MAX_COMMIT_METADATA_PAGES:
                complete = False
                break
            page = int(next_page)
        if not commits or all(
            commit.sha.casefold() != event.head_sha.casefold()
            for commit in commits
        ):
            complete = False
        bounded = await _with_verified_signatures(
            active_client,
            event,
            commits[:MAX_PULL_REQUEST_COMMITS],
        )
        return PullRequestCommits(commits=tuple(bounded), complete=complete)

    if client is not None:
        return await fetch(client)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await fetch(owned_client)


def _comparison_diff(value: object, event: PullRequestEvent) -> str | None:
    if not isinstance(value, dict):
        raise RuntimeError("GitLab returned an invalid comparison")
    commit = value.get("commit")
    commit_id = commit.get("id") if isinstance(commit, dict) else None
    if (
        not isinstance(commit_id, str)
        or commit_id.casefold() != event.head_sha.casefold()
    ):
        raise RuntimeError("GitLab comparison did not end at the reviewed head")
    diffs = value.get("diffs")
    if not isinstance(diffs, list):
        raise RuntimeError("GitLab returned invalid comparison diffs")
    if value.get("compare_timeout") is True:
        return None

    parts: list[str] = []
    for item in diffs:
        if not isinstance(item, dict):
            raise RuntimeError("GitLab returned an invalid comparison diff")
        old_path = item.get("old_path")
        new_path = item.get("new_path")
        diff = item.get("diff")
        if (
            not isinstance(old_path, str)
            or not isinstance(new_path, str)
            or not isinstance(diff, str)
        ):
            raise RuntimeError("GitLab returned an invalid comparison diff")
        if item.get("collapsed") is True or item.get("too_large") is True:
            return None
        if diff.startswith("diff --git "):
            parts.append(diff)
            continue
        old_label = "/dev/null" if item.get("new_file") is True else f"a/{old_path}"
        new_label = "/dev/null" if item.get("deleted_file") is True else f"b/{new_path}"
        parts.append(
            "\n".join(
                (
                    f"diff --git a/{old_path} b/{new_path}",
                    f"--- {old_label}",
                    f"+++ {new_label}",
                    diff,
                )
            )
        )
    rendered = "\n".join(parts)
    if len(rendered.encode()) > MAX_DIFF_BYTES:
        return None
    return rendered


async def fetch_gitlab_pull_request_update_diff(
    event: PullRequestEvent,
    previous_head_sha: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    if event.provider != "gitlab":
        raise ValueError("GitLab diff reader received a non-GitLab event")
    if not COMMIT_SHA_PATTERN.fullmatch(previous_head_sha):
        raise ValueError("Previous review head must be a full commit digest")
    if previous_head_sha.casefold() == event.head_sha:
        return ""
    url = (
        f"{event.api_base_url}/projects/{_source_project_path(event)}/"
        "repository/compare"
    )

    async def fetch(active_client: httpx.AsyncClient) -> str:
        try:
            content = await _fetch_bytes(
                active_client,
                url,
                headers=_headers(),
                params={
                    "from": previous_head_sha,
                    "to": event.head_sha,
                    "straight": "true",
                    "unidiff": "true",
                },
                limit=MAX_RESPONSE_BYTES,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {400, 404, 422}:
                raise
            # Fail closed for finding continuity: a full MR diff would treat
            # untouched files as changed and falsely address open findings.
            return ""
        try:
            value = httpx.Response(200, content=content).json()
        except ValueError as error:
            raise RuntimeError("GitLab returned a non-JSON comparison") from error
        rendered = _comparison_diff(value, event)
        if rendered is None:
            return ""
        return rendered

    if client is not None:
        return await fetch(client)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await fetch(owned_client)


async def _find_existing_note(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    marker: str,
) -> PublishedReview | None:
    url = f"{_merge_request_path(event)}/notes"
    for page in range(1, 21):
        response = await client.get(
            url,
            headers=_headers(),
            params={
                "order_by": "created_at",
                "sort": "asc",
                "per_page": 100,
                "page": page,
            },
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise RuntimeError("GitLab note list exceeds Diffuse's size limit")
        notes = response.json()
        if not isinstance(notes, list):
            raise RuntimeError("GitLab returned an invalid merge-request note list")
        for note in notes:
            body = note.get("body") if isinstance(note, dict) else None
            if isinstance(body, str) and marker in body:
                external_id = note.get("id")
                if external_id is None:
                    raise RuntimeError("GitLab note is missing its identifier")
                return PublishedReview(
                    external_id=str(external_id),
                    external_url=(
                        f"{event.web_url}#note_{external_id}"
                    ),
                    inline_comments_attached=(
                        "<!-- diffuse-inline-comments:fallback -->" not in body
                    ),
                )
        if len(notes) < 100:
            break
    return None


def _discussion_position(
    event: PullRequestEvent,
    parsed_diff: ParsedDiff,
    finding: ReviewFinding,
) -> dict[str, str] | None:
    file = parsed_diff.file(finding.file_path)
    if file is None or not file.contains(finding.side, finding.line):
        return None
    old_path = file.old_path or file.new_path
    new_path = file.new_path or file.old_path
    if old_path is None or new_path is None:
        return None
    position = {
        "position[base_sha]": event.base_sha,
        "position[head_sha]": event.head_sha,
        "position[start_sha]": event.start_sha,
        "position[position_type]": "text",
        "position[old_path]": old_path,
        "position[new_path]": new_path,
    }
    if finding.side == "RIGHT":
        position["position[new_line]"] = str(finding.line)
    else:
        position["position[old_line]"] = str(finding.line)
    return position


def _published_discussion(
    event: PullRequestEvent,
    value: object,
) -> PublishedFindingComment | None:
    if not isinstance(value, dict):
        raise RuntimeError("GitLab returned an invalid merge-request discussion")
    discussion_id = value.get("id")
    notes = value.get("notes")
    if not isinstance(discussion_id, str) or not discussion_id:
        raise RuntimeError("GitLab discussion is missing its identifier")
    if not isinstance(notes, list) or not notes:
        raise RuntimeError("GitLab discussion has no root note")
    root = notes[0]
    if not isinstance(root, dict):
        raise RuntimeError("GitLab discussion has an invalid root note")
    body = root.get("body")
    match = FINDING_MARKER_PATTERN.search(body) if isinstance(body, str) else None
    if match is None:
        return None
    note_id = root.get("id")
    if note_id is None:
        raise RuntimeError("GitLab discussion root note has no identifier")
    external_url = root.get("url")
    if not isinstance(external_url, str):
        external_url = f"{event.web_url}#note_{note_id}"
    return PublishedFindingComment(
        fingerprint=match.group(1),
        external_id=str(note_id),
        external_node_id=None,
        external_url=external_url,
        thread_id=discussion_id,
    )


async def _published_finding_discussions(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
) -> dict[str, PublishedFindingComment]:
    comments: dict[str, PublishedFindingComment] = {}
    url = f"{_merge_request_path(event)}/discussions"
    for page in range(1, MAX_DISCUSSION_PAGES + 1):
        response = await client.get(
            url,
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise RuntimeError("GitLab discussion list exceeds Diffuse's size limit")
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError("GitLab returned an invalid discussion list")
        for discussion in value:
            comment = _published_discussion(event, discussion)
            if comment is not None:
                comments.setdefault(comment.fingerprint, comment)
        if len(value) < 100:
            break
    return comments


async def _create_finding_discussion(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    *,
    finding: ReviewFinding,
    report: ReviewReport,
    review_run_id: int,
    position: dict[str, str],
) -> PublishedFindingComment | None:
    response = await client.post(
        f"{_merge_request_path(event)}/discussions",
        headers=_headers(),
        data={
            "body": _finding_comment(
                finding,
                include_confidence=report.confidence_score_section_included,
                include_fix_guidance=report.fix_with_agent_enabled,
                review_run_id=review_run_id,
            ),
            **position,
        },
    )
    if response.status_code in {400, 422}:
        return None
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab created discussion exceeds Diffuse's size limit")
    comment = _published_discussion(event, response.json())
    if comment is None or comment.fingerprint != finding.fingerprint:
        raise RuntimeError("GitLab returned the wrong created finding discussion")
    return comment


async def _publish_finding_discussions(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    *,
    review_run_id: int,
    report: ReviewReport,
    continuity: ReviewContinuity | None,
    diff_text: str,
) -> tuple[tuple[PublishedFindingComment, ...], bool, tuple[str, ...]]:
    findings = _inline_findings(report, continuity)
    if not findings:
        return (), True, ()
    parsed_diff = parse_unified_diff(diff_text)
    published = await _published_finding_discussions(client, event)
    all_attached = True
    for finding in findings:
        if finding.fingerprint in published:
            continue
        position = _discussion_position(event, parsed_diff, finding)
        if position is None:
            all_attached = False
            continue
        comment = await _create_finding_discussion(
            client,
            event,
            finding=finding,
            report=report,
            review_run_id=review_run_id,
            position=position,
        )
        if comment is None:
            all_attached = False
            continue
        published[finding.fingerprint] = comment
    expected = {finding.fingerprint for finding in findings}
    all_attached = all_attached and expected.issubset(published)
    return (
        tuple(
            published[fingerprint]
            for fingerprint in sorted(expected & published.keys())
        ),
        all_attached,
        tuple(sorted(expected - published.keys())),
    )


def _validated_merge_request(
    response: httpx.Response,
    event: PullRequestEvent,
) -> dict[str, object]:
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab merge-request response exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("GitLab returned an invalid merge request")
    description = value.get("description")
    sha = value.get("sha")
    if (
        value.get("iid") != event.number
        or value.get("web_url") != event.web_url
        or value.get("state") != "opened"
        or not isinstance(sha, str)
        or sha.casefold() != event.head_sha.casefold()
        or description is not None
        and not isinstance(description, str)
    ):
        raise RuntimeError(
            "GitLab merge request no longer matches the reviewed open revision"
        )
    return value


async def _update_merge_request_description(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    *,
    review_run_id: int,
    report: ReviewReport,
    review_number: int,
    inline_comments_attached: bool,
    continuity: ReviewContinuity | None,
) -> None:
    url = _merge_request_path(event)
    current_response = await client.get(url, headers=_headers())
    current = _validated_merge_request(current_response, event)
    existing = current.get("description") or ""
    review_body = format_review_body(
        review_run_id,
        event.head_sha,
        report,
        review_number=review_number,
        commit_url=_commit_url(event),
        inline_comments_attached=inline_comments_attached,
        continuity=continuity,
        visible_content=True,
        inline_fallback_message=(
            "GitLab could not attach every validated finding to its exact "
            "diff line, so the complete findings are included below."
        ),
        rerun_instruction="",
    )
    merged = merge_review_description(
        existing,
        review_body,
        max_chars=MAX_MERGE_REQUEST_DESCRIPTION_CHARS,
    )
    if merged == existing:
        return
    updated_response = await client.put(
        url,
        headers=_headers(),
        json={"description": merged},
    )
    updated = _validated_merge_request(updated_response, event)
    if updated.get("description") != merged:
        raise RuntimeError(
            "GitLab did not persist the managed Diffuse description region"
        )


async def _post_gitlab_failure_notice(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    failure: TerminalReviewFailure,
) -> str:
    body = format_failure_notice(failure)
    existing = await _find_existing_note(client, event, failure.marker)
    if existing is not None:
        # Edit the single notice rather than appending another. The marker is
        # per-merge-request, so this covers a later failing job as well as a
        # retry, and the body carries the current job id.
        updated = await client.put(
            f"{_merge_request_path(event)}/notes/"
            f"{quote(existing.external_id, safe='')}",
            headers=_headers(),
            json={"body": body},
        )
        updated.raise_for_status()
        return existing.external_id
    response = await client.post(
        f"{_merge_request_path(event)}/notes",
        headers=_headers(),
        json={"body": body},
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitLab returned an invalid created note")
    return str(value["id"])


async def post_gitlab_review_failure_notice(
    event: PullRequestEvent,
    *,
    failure: TerminalReviewFailure,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Post one terminal-failure notice, reusing any notice already present.

    The Diffuse-owned marker on the note body is the idempotency key: a
    replayed terminal path finds its own earlier notice and posts nothing.
    """
    if event.provider != "gitlab":
        raise ValueError("GitLab failure notice received a non-GitLab event")
    if client is not None:
        return await _post_gitlab_failure_notice(client, event, failure)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await _post_gitlab_failure_notice(owned_client, event, failure)


async def publish_gitlab_review(
    event: PullRequestEvent,
    *,
    review_run_id: int,
    report: ReviewReport,
    diff_text: str = "",
    review_number: int = 1,
    continuity: ReviewContinuity | None = None,
    client: httpx.AsyncClient | None = None,
) -> PublishedReview:
    if event.provider != "gitlab":
        raise ValueError("GitLab publisher received a non-GitLab event")
    if not report.publication_enabled:
        raise ValueError("Repository policy disabled publication for this review")
    marker = f"<!-- diffuse-review:{review_run_id}:{event.head_sha} -->"

    async def publish(active_client: httpx.AsyncClient) -> PublishedReview:
        finding_comments, inline_comments_attached, unattached_fingerprints = (
            await _publish_finding_discussions(
                active_client,
                event,
                review_run_id=review_run_id,
                report=report,
                continuity=continuity,
                diff_text=diff_text,
            )
        )
        publish_summary_note = (
            report.summary_comment_enabled and not report.update_description
        )
        if publish_summary_note:
            existing = await _find_existing_note(
                active_client,
                event,
                marker,
            )
            if existing is not None:
                expected = {
                    finding.fingerprint
                    for finding in _inline_findings(report, continuity)
                }
                found = {comment.fingerprint for comment in finding_comments}
                if (
                    existing.inline_comments_attached
                    and not expected.issubset(found)
                ):
                    raise RuntimeError(
                        "Published GitLab finding discussions are not yet visible"
                    )
                published = PublishedReview(
                    external_id=existing.external_id,
                    external_url=existing.external_url,
                    finding_comments=finding_comments,
                    inline_comments_attached=existing.inline_comments_attached,
                    unattached_fingerprints=unattached_fingerprints,
                )
            else:
                body = format_review_body(
                    review_run_id,
                    event.head_sha,
                    report,
                    review_number=review_number,
                    commit_url=_commit_url(event),
                    inline_comments_attached=inline_comments_attached,
                    continuity=continuity,
                    inline_fallback_message=(
                        "GitLab could not attach every validated finding to its "
                        "exact diff line, so the complete findings are included below."
                    ),
                    rerun_instruction="",
                )
                response = await active_client.post(
                    f"{_merge_request_path(event)}/notes",
                    headers=_headers(),
                    json={
                        "body": body,
                        "merge_request_diff_head_sha": event.head_sha,
                    },
                )
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise RuntimeError(
                        "GitLab created-note response exceeds Diffuse's size limit"
                    )
                value = response.json()
                if not isinstance(value, dict) or value.get("id") is None:
                    raise RuntimeError(
                        "GitLab returned an invalid created merge-request note"
                    )
                external_id = str(value["id"])
                published = PublishedReview(
                    external_id=external_id,
                    external_url=f"{event.web_url}#note_{external_id}",
                    finding_comments=finding_comments,
                    inline_comments_attached=inline_comments_attached,
                    unattached_fingerprints=unattached_fingerprints,
                )
        else:
            prefix = "description" if report.update_description else "silent"
            published = PublishedReview(
                external_id=f"{prefix}:{event.number}:{event.head_sha}",
                external_url=event.web_url,
                finding_comments=finding_comments,
                inline_comments_attached=inline_comments_attached,
                unattached_fingerprints=unattached_fingerprints,
            )
        if report.update_description:
            await _update_merge_request_description(
                active_client,
                event,
                review_run_id=review_run_id,
                report=report,
                review_number=review_number,
                inline_comments_attached=published.inline_comments_attached,
                continuity=continuity,
            )
        return published

    if client is not None:
        return await publish(client)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await publish(owned_client)
