"""Idempotent GitHub check-run publication for native Diffuse reviews."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from service.check_store import CHECK_NAME, VALID_CONCLUSIONS, CheckConclusion
from service.github import GITHUB_API_VERSION
from service.github_app import github_token
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

MAX_CHECK_SUMMARY_CHARS = 60_000
MAX_CHECK_ANNOTATIONS = 50
MAX_ANNOTATION_MESSAGE_CHARS = 4_000


@dataclass(frozen=True)
class PublishedCheckRun:
    external_id: str
    external_url: str | None


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_text(value: str) -> str:
    return value.replace("@", "@\u200b")


def _headers() -> dict[str, str]:
    token = github_token()
    if not token:
        raise RuntimeError(
            "GitHub authentication with Checks write permission is required"
        )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION",
            GITHUB_API_VERSION,
        ),
        "User-Agent": "diffuse-check-run",
    }


def _repository_api_path(event: PullRequestEvent) -> str:
    owner, repository = event.repo_full_name.split("/", maxsplit=1)
    return (
        f"{event.api_base_url}/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}"
    )


def _check_runs_url(event: PullRequestEvent) -> str:
    return f"{_repository_api_path(event)}/check-runs"


async def _find_existing_check_run(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    external_key: str,
) -> PublishedCheckRun | None:
    url = (
        f"{_repository_api_path(event)}/commits/"
        f"{quote(event.head_sha, safe='')}/check-runs"
    )
    for page in range(1, 11):
        response = await client.get(
            url,
            headers=_headers(),
            params={
                "check_name": CHECK_NAME,
                "filter": "all",
                "per_page": 100,
                "page": page,
            },
        )
        response.raise_for_status()
        value = response.json()
        check_runs = value.get("check_runs") if isinstance(value, dict) else None
        if not isinstance(check_runs, list):
            raise RuntimeError("GitHub returned an invalid check-run list")
        for check_run in check_runs:
            if (
                isinstance(check_run, dict)
                and check_run.get("external_id") == external_key
            ):
                external_id = check_run.get("id")
                if external_id is None:
                    raise RuntimeError("GitHub check run is missing its identifier")
                return PublishedCheckRun(
                    external_id=str(external_id),
                    external_url=check_run.get("html_url"),
                )
        if len(check_runs) < 100:
            break
    return None


async def ensure_github_check_run(
    event: PullRequestEvent,
    *,
    external_key: str,
    existing_external_id: str | None = None,
    existing_external_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> PublishedCheckRun:
    if event.provider != "github":
        raise ValueError("GitHub check publisher received a non-GitHub event")
    if existing_external_id:
        return PublishedCheckRun(existing_external_id, existing_external_url)
    timeout = scm_api_timeout_seconds()
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _ensure_with_client(owned_client, event, external_key)
    return await _ensure_with_client(client, event, external_key)


async def _ensure_with_client(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    external_key: str,
) -> PublishedCheckRun:
    existing = await _find_existing_check_run(client, event, external_key)
    if existing:
        return existing
    response = await client.post(
        _check_runs_url(event),
        headers=_headers(),
        json={
            "name": CHECK_NAME,
            "head_sha": event.head_sha,
            "status": "in_progress",
            "external_id": external_key,
            "started_at": _timestamp(),
            "output": {
                "title": "Diffuse review in progress",
                "summary": (
                    f"Analyzing pull request #{event.number} at "
                    f"`{event.head_sha[:12]}`."
                ),
            },
        },
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitHub returned an invalid created check run")
    return PublishedCheckRun(
        external_id=str(value["id"]),
        external_url=value.get("html_url"),
    )


def review_check_conclusion(
    report: ReviewReport,
    blocking_severities: tuple[str, ...],
    *,
    unresolved_findings: tuple[ReviewFinding, ...] | None = None,
) -> CheckConclusion:
    if not report.publication_enabled:
        return "skipped"
    if any(
        finding.severity.value in blocking_severities
        for finding in (
            unresolved_findings
            if unresolved_findings is not None
            else tuple(report.findings)
        )
    ):
        return "failure"
    return "success"


def _annotation(
    finding: ReviewFinding,
    blocking_severities: tuple[str, ...],
) -> dict[str, object]:
    security_label = ""
    if finding.category is Category.SECURITY:
        security_label = (
            "Preventative security risk"
            if finding.security_classification
            is SecurityClassification.PREVENTATIVE
            else "Security vulnerability"
        )
    message = "\n\n".join(
        tuple(
            item
            for item in (
                security_label,
                _safe_text(finding.body),
                f"Evidence: {_safe_text(finding.evidence)}",
                f"Confidence: {finding.confidence:.0%}",
            )
            if item
        )
    )
    return {
        "path": finding.file_path,
        "start_line": finding.line,
        "end_line": finding.line,
        "annotation_level": (
            "failure"
            if finding.severity.value in blocking_severities
            else "warning"
        ),
        "title": _safe_text(
            f"[{finding.severity.value.upper()}] "
            f"{f'{security_label}: ' if security_label else ''}{finding.title}"
        )[:255],
        "message": message[:MAX_ANNOTATION_MESSAGE_CHARS],
    }


def _annotations(
    report: ReviewReport,
    active_findings: tuple[ReviewFinding, ...],
    blocking_severities: tuple[str, ...],
) -> list[dict[str, object]]:
    """Annotate this run's findings plus still-open lineages it did not re-emit.

    The conclusion is derived from ``active_findings``, so a check can go red for
    an older open lineage. Annotating only ``report.findings`` would leave that
    check red with nothing shown in the Files tab.

    ``MAX_CHECK_ANNOTATIONS`` is a hard GitHub-side cap, and truncating in source
    order let a full cap of non-blocking findings from this run crowd out the one
    blocking lineage that turned the check red — the same empty-Files-tab symptom
    at a different layer. Blocking findings therefore win a slot first.

    Selection is reordered; presentation is not. Once the surviving set is
    chosen, it is emitted in the original this-run-then-still-open order, so the
    common under-cap case looks exactly as before.
    """
    candidates: list[ReviewFinding] = []
    seen: set[str] = set()
    for finding in (*report.findings, *active_findings):
        if finding.side != "RIGHT" or finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        candidates.append(finding)

    if len(candidates) > MAX_CHECK_ANNOTATIONS:
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (
                item[1].severity.value not in blocking_severities,
                item[0],
            ),
        )
        kept = sorted(index for index, _ in ranked[:MAX_CHECK_ANNOTATIONS])
        candidates = [candidates[index] for index in kept]

    return [
        _annotation(finding, blocking_severities) for finding in candidates
    ]


def _completion_output(
    report: ReviewReport | None,
    conclusion: CheckConclusion,
    blocking_severities: tuple[str, ...],
    message: str | None,
    unresolved_findings: tuple[ReviewFinding, ...] | None,
) -> dict[str, object]:
    if report is None:
        return {
            "title": {
                "cancelled": "Diffuse review superseded",
                "failure": "Diffuse review failed",
                "neutral": "Diffuse review completed",
                "skipped": "Diffuse review skipped",
                "success": "Diffuse review passed",
            }[conclusion],
            "summary": _safe_text(
                message or "Diffuse review reached a terminal state."
            )[:MAX_CHECK_SUMMARY_CHARS],
        }
    active_findings = (
        unresolved_findings
        if unresolved_findings is not None
        else tuple(report.findings)
    )
    blocking_count = sum(
        finding.severity.value in blocking_severities
        for finding in active_findings
    )
    summary = (
        f"{_safe_text(report.summary)}\n\n"
        f"Confidence: **{report.confidence_score}/5** · "
        f"Risk: **{report.risk_score:.1f}/10** · "
        f"Active findings: **{len(active_findings)}** · "
        f"Blocking: **{blocking_count}** · "
        f"Reviewed files: **{report.reviewed_file_count}/{report.diff_file_count}**"
    )
    annotations = _annotations(report, active_findings, blocking_severities)
    output: dict[str, object] = {
        "title": (
            f"Diffuse found {blocking_count} blocking "
            f"{'finding' if blocking_count == 1 else 'findings'}"
            if blocking_count
            else "Diffuse review passed"
        ),
        "summary": summary[:MAX_CHECK_SUMMARY_CHARS],
    }
    if annotations:
        output["annotations"] = annotations
    return output


async def complete_github_check_run(
    event: PullRequestEvent,
    *,
    external_id: str,
    conclusion: CheckConclusion,
    blocking_severities: tuple[str, ...] = ("critical", "high"),
    report: ReviewReport | None = None,
    message: str | None = None,
    unresolved_findings: tuple[ReviewFinding, ...] | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    if event.provider != "github":
        raise ValueError("GitHub check publisher received a non-GitHub event")
    if conclusion not in VALID_CONCLUSIONS:
        raise ValueError("Invalid GitHub check-run conclusion")
    timeout = scm_api_timeout_seconds()
    payload = {
        "name": CHECK_NAME,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": _timestamp(),
        "output": _completion_output(
            report,
            conclusion,
            blocking_severities,
            message,
            unresolved_findings,
        ),
    }
    url = f"{_check_runs_url(event)}/{quote(external_id, safe='')}"
    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            response = await owned_client.patch(
                url,
                headers=_headers(),
                json=payload,
            )
    else:
        response = await client.patch(
            url,
            headers=_headers(),
            json=payload,
        )
    response.raise_for_status()
