"""Idempotent publication of validated native reviews to GitHub."""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from service.finding_lineage import ReviewContinuity
from service.finding_store import PublishedFindingComment
from service.github import GITHUB_API_VERSION
from service.review_description import merge_review_description
from service.review_failure_notice import (
    TerminalReviewFailure,
    format_failure_notice,
)
from service.review_models import (
    Category,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
)
from service.scm import (
    PullRequestEvent,
    scm_api_timeout_seconds,
)

MAX_INLINE_COMMENTS = 25
MAX_REVIEW_BODY_CHARS = 60_000
MAX_INLINE_BODY_CHARS = 10_000
MAX_PULL_REQUEST_RESPONSE_BYTES = 1_000_000
MAX_PULL_REQUEST_DESCRIPTION_CHARS = 65_536
FINDING_MARKER_PATTERN = re.compile(
    r"<!-- diffuse-finding:([0-9a-f]{64}) -->"
)


@dataclass(frozen=True)
class PublishedReview:
    external_id: str
    external_url: str | None
    finding_comments: tuple[PublishedFindingComment, ...] = ()
    inline_comments_attached: bool = True
    # Fingerprints of `new` findings this publication tried and failed to anchor
    # to an inline thread. Findings the provider never attempts (summary-only
    # policy, inline caps) are absent, so activation only withholds lineages
    # whose promised root thread is genuinely missing.
    unattached_fingerprints: tuple[str, ...] = ()


def _safe_markdown(value: str) -> str:
    return value.replace("@", "@\u200b")


def _table_cell(value: str) -> str:
    return _safe_markdown(value).replace("|", "\\|").replace("\n", " ")


def _security_badge(finding: ReviewFinding) -> str:
    if finding.category is not Category.SECURITY:
        return ""
    if finding.security_classification is SecurityClassification.PREVENTATIVE:
        return "🛡️ Preventative security risk"
    return "🔒 Security vulnerability"


def _finding_comment(
    finding: ReviewFinding,
    *,
    include_confidence: bool = True,
    include_fix_guidance: bool = True,
    review_run_id: int | None = None,
) -> str:
    badge = _security_badge(finding)
    parts = [
        (
            f"### {badge} · [{finding.severity.value.upper()}] "
            f"{_safe_markdown(finding.title)}"
            if badge
            else f"### [{finding.severity.value.upper()}] "
            f"{_safe_markdown(finding.title)}"
        ),
        _safe_markdown(finding.body),
    ]
    parts.append(f"**Evidence:** {_safe_markdown(finding.evidence)}")
    metadata = f"**Category:** `{finding.category.value}`"
    if include_confidence:
        metadata = f"**Confidence:** {finding.confidence:.0%} · {metadata}"
    parts.append(metadata)
    if include_fix_guidance and finding.suggested_fix:
        parts.append(f"**Suggested fix:**\n\n{_safe_markdown(finding.suggested_fix)}")
    handoff = ""
    if include_fix_guidance and review_run_id is not None:
        handoff = _output_section(
            "Fix with your agent",
            (
                "Use the Diffuse MCP tool "
                f"`get_fix_handoff` with `codeReviewId=review_{review_run_id}` and "
                f"`findingFingerprint={finding.fingerprint}`."
            ),
            collapsible=True,
            default_open=False,
        )
    marker = f"<!-- diffuse-finding:{finding.fingerprint} -->"
    suffix = f"{handoff}\n\n{marker}" if handoff else marker
    content_limit = MAX_INLINE_BODY_CHARS - len(suffix) - 2
    content = "\n\n".join(parts)[:content_limit].rstrip()
    return f"{content}\n\n{suffix}"


def _output_section(
    title: str,
    body: str,
    *,
    collapsible: bool,
    default_open: bool,
) -> str:
    if not collapsible:
        return body
    open_attribute = " open" if default_open else ""
    safe_title = html.escape(title, quote=True)
    return (
        f"<details{open_attribute}>\n"
        f"<summary><strong>{safe_title}</strong></summary>\n\n"
        f"{body}\n\n"
        "</details>"
    )


def _diagram_section(report: ReviewReport) -> str | None:
    diagram = report.diagram
    if diagram is None:
        return None
    title = _safe_markdown(html.escape(diagram.title, quote=True))
    body = f"```mermaid\n{diagram.mermaid}\n```"
    if not report.diagram_collapsible:
        return f"### Change diagram — {title}\n\n{body}"
    open_attribute = " open" if report.diagram_default_open else ""
    return (
        f"<details{open_attribute}>\n"
        f"<summary><strong>Change diagram — {title}</strong></summary>\n\n"
        f"{body}\n\n"
        "</details>"
    )


def format_review_body(
    review_run_id: int,
    head_sha: str,
    report: ReviewReport,
    *,
    review_number: int = 1,
    commit_url: str | None = None,
    inline_comments_attached: bool = True,
    continuity: ReviewContinuity | None = None,
    visible_content: bool | None = None,
    inline_fallback_message: str = (
        "GitHub could not attach the inline annotations, so the validated "
        "findings are included below."
    ),
    rerun_instruction: str = "Reply `@diffuse review` to re-run. ",
) -> str:
    if review_number <= 0:
        raise ValueError("Review number must be positive")
    marker = f"<!-- diffuse-review:{review_run_id}:{head_sha} -->"
    inline_marker = (
        "<!-- diffuse-inline-comments:attached -->"
        if inline_comments_attached
        else "<!-- diffuse-inline-comments:fallback -->"
    )
    if visible_content is None:
        visible_content = (
            report.summary_comment_enabled and not report.update_description
        )
    if not visible_content:
        return f"{marker}\n{inline_marker}"
    coverage = (
        f"{report.reviewed_file_count}/{report.diff_file_count} changed files"
        if report.diff_file_count
        else "no textual changed files"
    )
    parts = [
        marker,
        inline_marker,
        "## Diffuse code review",
    ]
    if report.summary_section_included:
        parts.append(
            _output_section(
                "Summary",
                _safe_markdown(report.summary),
                collapsible=report.summary_section_collapsible,
                default_open=report.summary_section_default_open,
            )
        )
    parts.append(
        f"**Risk:** {report.risk_score:.1f}/10 · "
        f"**Findings:** {len(report.findings)} · **Coverage:** {coverage}"
    )
    if report.confidence_score_section_included:
        parts.append(
            _output_section(
                "Confidence score",
                f"**Confidence:** {report.confidence_score}/5",
                collapsible=report.confidence_score_section_collapsible,
                default_open=report.confidence_score_section_default_open,
            )
        )
    if continuity is not None:
        earlier_open_count = max(
            0,
            len(continuity.open_findings)
            - len(continuity.new_fingerprints)
            - len(continuity.reopened_fingerprints),
        )
        parts.append(
            "**Finding changes:** "
            f"{len(continuity.new_fingerprints)} new · "
            f"{earlier_open_count} still open · "
            f"{len(continuity.reopened_fingerprints)} reopened · "
            f"{len(continuity.addressed)} addressed"
        )
    diagram_section = _diagram_section(report)
    if diagram_section is not None:
        parts.append(diagram_section)
    if report.findings:
        if report.issues_table_section_included:
            table = [
                "| Severity | Finding | Location | Confidence |",
                "| --- | --- | --- | --- |",
            ]
            if not report.confidence_score_section_included:
                table = [
                    "| Severity | Finding | Location |",
                    "| --- | --- | --- |",
                ]
            for finding in report.findings:
                badge = _security_badge(finding)
                row = (
                    "| "
                    f"{finding.severity.value.upper()} | "
                    f"{_table_cell(f'{badge} · {finding.title}' if badge else finding.title)} | "
                    f"`{_table_cell(finding.file_path)}:{finding.line}` |"
                )
                if report.confidence_score_section_included:
                    row = f"{row} {finding.confidence:.0%} |"
                table.append(row)
            parts.append(
                _output_section(
                    "Issues",
                    "\n".join(table),
                    collapsible=report.issues_table_section_collapsible,
                    default_open=report.issues_table_section_default_open,
                )
            )
        if report.fix_with_agent_enabled:
            parts.append(
                _output_section(
                    "Fix all with your agent",
                    (
                        "Use the Diffuse MCP tool "
                        f"`get_fix_all_handoff` with `codeReviewId=review_{review_run_id}`. "
                        "The handoff refuses stale review revisions."
                    ),
                    collapsible=True,
                    default_open=False,
                )
            )
        if not inline_comments_attached or not report.inline_comments_enabled:
            if report.inline_comments_enabled:
                parts.append(f"\n{_safe_markdown(inline_fallback_message)}")
            else:
                parts.append(
                    "\nRepository policy requested summary-only review, so the validated "
                    "findings are included below."
                )
            for finding in report.findings:
                badge = _security_badge(finding)
                parts.append(
                    f"\n### `{finding.file_path}:{finding.line}` — "
                    f"{_safe_markdown(f'{badge} · {finding.title}' if badge else finding.title)}"
                    "\n\n"
                    f"{_safe_markdown(finding.body)}"
                )
    if continuity is not None and continuity.addressed:
        parts.append("### Addressed since the previous review")
        parts.extend(
            (
                f"- `{item.file_path}:{item.line}` — "
                f"{_safe_markdown(item.title)}"
            )
            for item in continuity.addressed
        )
    commit = f"`{head_sha[:12]}`"
    if commit_url is not None:
        commit = f"[`{head_sha[:12]}`]({commit_url})"
    if report.footer_included:
        parts.append(
            "\n---\n"
            f"<sub>Review {review_number} · Last reviewed commit {commit} · "
            f"{rerun_instruction}"
            "Generated by Diffuse's native "
            "structured review engine; repository content was treated as untrusted "
            "input.</sub>"
        )
    return "\n\n".join(parts)[:MAX_REVIEW_BODY_CHARS]


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to publish reviews")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-native-review",
    }


def _pull_request_url(event: PullRequestEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{event.number}"
    )


def _reviews_url(event: PullRequestEvent) -> str:
    return f"{_pull_request_url(event)}/reviews"


def _commit_url(event: PullRequestEvent) -> str:
    return (
        f"{event.scm_base_url}/{quote(event.repo_full_name, safe='/')}/"
        f"commit/{event.head_sha}"
    )


async def _find_existing_review(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    marker: str,
) -> PublishedReview | None:
    url = _reviews_url(event)
    for page in range(1, 21):
        response = await client.get(
            url,
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        reviews = response.json()
        if not isinstance(reviews, list):
            raise RuntimeError("GitHub returned an invalid review list")
        for review in reviews:
            body = review.get("body") if isinstance(review, dict) else None
            if isinstance(body, str) and marker in body:
                external_id = review.get("id")
                if external_id is None:
                    raise RuntimeError("GitHub review is missing its identifier")
                return PublishedReview(
                    external_id=str(external_id),
                    external_url=review.get("html_url"),
                    inline_comments_attached=(
                        "<!-- diffuse-inline-comments:fallback -->" not in body
                    ),
                )
        if len(reviews) < 100:
            break
    return None


def _inline_findings(
    report: ReviewReport,
    continuity: ReviewContinuity | None,
) -> list[ReviewFinding]:
    if not report.inline_comments_enabled:
        return []
    selected = (
        continuity.inline_fingerprints
        if continuity is not None
        else frozenset(finding.fingerprint for finding in report.findings)
    )
    return [
        finding
        for finding in report.findings
        if finding.fingerprint in selected
    ][:MAX_INLINE_COMMENTS]


def _inline_comments(
    report: ReviewReport,
    continuity: ReviewContinuity | None,
    *,
    review_run_id: int,
) -> list[dict[str, object]]:
    return [
        {
            "path": finding.file_path,
            "line": finding.line,
            "side": finding.side,
            "body": _finding_comment(
                finding,
                include_confidence=report.confidence_score_section_included,
                include_fix_guidance=report.fix_with_agent_enabled,
                review_run_id=review_run_id,
            ),
        }
        for finding in _inline_findings(report, continuity)
    ]


def _review_comments_url(
    event: PullRequestEvent,
    review_id: str,
) -> str:
    return f"{_reviews_url(event)}/{quote(review_id, safe='')}/comments"


async def _published_finding_comments(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    review_id: str,
) -> tuple[PublishedFindingComment, ...]:
    comments: dict[str, PublishedFindingComment] = {}
    url = _review_comments_url(event, review_id)
    for page in range(1, 11):
        response = await client.get(
            url,
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError("GitHub returned an invalid review-comment list")
        for comment in value:
            body = comment.get("body") if isinstance(comment, dict) else None
            match = FINDING_MARKER_PATTERN.search(body) if isinstance(body, str) else None
            if not match:
                continue
            external_id = comment.get("id")
            if external_id is None:
                raise RuntimeError("GitHub review comment is missing its identifier")
            comments.setdefault(
                match.group(1),
                PublishedFindingComment(
                    fingerprint=match.group(1),
                    external_id=str(external_id),
                    external_node_id=comment.get("node_id"),
                    external_url=comment.get("html_url"),
                ),
            )
        if len(value) < 100:
            break
    return tuple(comments[key] for key in sorted(comments))


def _issue_comments_url(event: PullRequestEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/issues/{event.number}/comments"
    )


async def _find_existing_issue_comment(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    marker: str,
) -> str | None:
    url = _issue_comments_url(event)
    for page in range(1, 21):
        response = await client.get(
            url,
            headers=_headers(),
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        comments = response.json()
        if not isinstance(comments, list):
            raise RuntimeError("GitHub returned an invalid issue comment list")
        for comment in comments:
            body = comment.get("body") if isinstance(comment, dict) else None
            if isinstance(body, str) and marker in body:
                external_id = comment.get("id")
                if external_id is None:
                    raise RuntimeError("GitHub comment is missing its identifier")
                return str(external_id)
        if len(comments) < 100:
            break
    return None


async def _post_github_failure_notice(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    failure: TerminalReviewFailure,
) -> str:
    body = format_failure_notice(failure)
    existing = await _find_existing_issue_comment(client, event, failure.marker)
    if existing is not None:
        # Edit the single notice rather than appending another. The marker is
        # per-pull-request, so this covers a later failing job as well as a retry,
        # and the body carries the current job id.
        owner, repository = event.repo_full_name.split("/", maxsplit=1)
        updated = await client.patch(
            f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
            f"{quote(repository, safe='')}/issues/comments/"
            f"{quote(existing, safe='')}",
            headers=_headers(),
            json={"body": body},
        )
        updated.raise_for_status()
        return existing
    response = await client.post(
        _issue_comments_url(event),
        headers=_headers(),
        json={"body": body},
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitHub returned an invalid created issue comment")
    return str(value["id"])


async def post_github_review_failure_notice(
    event: PullRequestEvent,
    *,
    failure: TerminalReviewFailure,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Post one terminal-failure notice, reusing any notice already present.

    The Diffuse-owned marker on the comment body is the idempotency key: a
    replayed terminal path finds its own earlier notice and posts nothing.
    """
    if event.provider != "github":
        raise ValueError("GitHub failure notice received a non-GitHub event")
    if client is not None:
        return await _post_github_failure_notice(client, event, failure)
    timeout = scm_api_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await _post_github_failure_notice(owned_client, event, failure)


def _validated_pull_request(
    response: httpx.Response,
    event: PullRequestEvent,
) -> dict[str, object]:
    response.raise_for_status()
    if len(response.content) > MAX_PULL_REQUEST_RESPONSE_BYTES:
        raise RuntimeError("GitHub pull-request response exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an invalid pull request")
    head = value.get("head")
    body = value.get("body")
    if (
        value.get("number") != event.number
        or value.get("html_url") != event.web_url
        or value.get("state") != "open"
        or not isinstance(head, dict)
        or not isinstance(head.get("sha"), str)
        or head["sha"].casefold() != event.head_sha.casefold()
        or body is not None
        and not isinstance(body, str)
    ):
        raise RuntimeError(
            "GitHub pull request no longer matches the reviewed open revision"
        )
    return value


async def _update_pull_request_description(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    *,
    review_run_id: int,
    report: ReviewReport,
    review_number: int,
    inline_comments_attached: bool,
    continuity: ReviewContinuity | None,
) -> None:
    url = _pull_request_url(event)
    response = await client.get(url, headers=_headers())
    value = _validated_pull_request(response, event)
    existing = value.get("body") or ""
    review_body = format_review_body(
        review_run_id,
        event.head_sha,
        report,
        review_number=review_number,
        commit_url=_commit_url(event),
        inline_comments_attached=inline_comments_attached,
        continuity=continuity,
        visible_content=True,
    )
    merged = merge_review_description(
        existing,
        review_body,
        max_chars=MAX_PULL_REQUEST_DESCRIPTION_CHARS,
    )
    if merged == existing:
        return
    headers = _headers()
    etag = response.headers.get("etag")
    if etag:
        headers["If-Match"] = etag
    updated_response = await client.patch(
        url,
        headers=headers,
        json={"body": merged},
    )
    if updated_response.status_code in {409, 412}:
        raise RuntimeError(
            "GitHub pull-request description changed during Diffuse publication"
        )
    updated = _validated_pull_request(updated_response, event)
    if updated.get("body") != merged:
        raise RuntimeError(
            "GitHub did not persist the managed Diffuse description region"
        )


async def publish_github_review(
    event: PullRequestEvent,
    *,
    review_run_id: int,
    report: ReviewReport,
    review_number: int = 1,
    continuity: ReviewContinuity | None = None,
    client: httpx.AsyncClient | None = None,
) -> PublishedReview:
    if event.provider != "github":
        raise ValueError("GitHub publisher received a non-GitHub event")
    if not report.publication_enabled:
        raise ValueError("Repository policy disabled publication for this review")

    marker = f"<!-- diffuse-review:{review_run_id}:{event.head_sha} -->"
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _publish_with_client(
                owned_client,
                event,
                review_run_id,
                report,
                review_number,
                marker,
                continuity,
            )
    return await _publish_with_client(
        client,
        event,
        review_run_id,
        report,
        review_number,
        marker,
        continuity,
    )


async def _publish_with_client(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    review_run_id: int,
    report: ReviewReport,
    review_number: int,
    marker: str,
    continuity: ReviewContinuity | None,
) -> PublishedReview:
    existing = await _find_existing_review(client, event, marker)
    if existing:
        finding_comments = await _published_finding_comments(
            client,
            event,
            existing.external_id,
        )
        expected = {
            finding.fingerprint
            for finding in _inline_findings(report, continuity)
        }
        found = {comment.fingerprint for comment in finding_comments}
        if (
            existing.inline_comments_attached
            and not expected.issubset(found)
        ):
            raise RuntimeError("Published GitHub review comments are not yet visible")
        published = PublishedReview(
            external_id=existing.external_id,
            external_url=existing.external_url,
            finding_comments=finding_comments,
            inline_comments_attached=existing.inline_comments_attached,
            unattached_fingerprints=tuple(sorted(expected - found)),
        )
        if report.update_description:
            await _update_pull_request_description(
                client,
                event,
                review_run_id=review_run_id,
                report=report,
                review_number=review_number,
                inline_comments_attached=published.inline_comments_attached,
                continuity=continuity,
            )
        return published

    comments = _inline_comments(
        report,
        continuity,
        review_run_id=review_run_id,
    )
    if not comments and (
        report.update_description or not report.summary_comment_enabled
    ):
        published = PublishedReview(
            external_id=f"silent:{event.number}:{event.head_sha}",
            external_url=event.web_url,
            inline_comments_attached=True,
        )
        if report.update_description:
            await _update_pull_request_description(
                client,
                event,
                review_run_id=review_run_id,
                report=report,
                review_number=review_number,
                inline_comments_attached=True,
                continuity=continuity,
            )
        return published
    payload: dict[str, object] = {
        "commit_id": event.head_sha,
        "body": format_review_body(
            review_run_id,
            event.head_sha,
            report,
            review_number=review_number,
            commit_url=_commit_url(event),
            continuity=continuity,
        ),
        "event": "COMMENT",
        "comments": comments,
    }
    response = await client.post(
        _reviews_url(event),
        headers=_headers(),
        json=payload,
    )
    if response.status_code == 422 and comments:
        existing = await _find_existing_review(client, event, marker)
        if existing:
            return await _publish_with_client(
                client,
                event,
                review_run_id,
                report,
                review_number,
                marker,
                continuity,
            )
        payload["comments"] = []
        payload["body"] = format_review_body(
            review_run_id,
            event.head_sha,
            report,
            review_number=review_number,
            commit_url=_commit_url(event),
            inline_comments_attached=False,
            continuity=continuity,
        )
        response = await client.post(
            _reviews_url(event),
            headers=_headers(),
            json=payload,
        )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitHub returned an invalid created review")
    review_id = str(value["id"])
    finding_comments = await _published_finding_comments(
        client,
        event,
        review_id,
    )
    expected = {
        finding.fingerprint
        for finding in _inline_findings(report, continuity)
    }
    found = {comment.fingerprint for comment in finding_comments}
    if payload["comments"] and not expected.issubset(found):
        raise RuntimeError("Created GitHub review comments are not yet visible")
    published = PublishedReview(
        external_id=review_id,
        external_url=value.get("html_url"),
        finding_comments=finding_comments,
        inline_comments_attached=bool(payload["comments"] or not expected),
        unattached_fingerprints=tuple(sorted(expected - found)),
    )
    if report.update_description:
        await _update_pull_request_description(
            client,
            event,
            review_run_id=review_run_id,
            report=report,
            review_number=review_number,
            inline_comments_attached=published.inline_comments_attached,
            continuity=continuity,
        )
    return published
