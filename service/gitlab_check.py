"""GitLab commit-status publication for native Diffuse reviews."""

from __future__ import annotations

import os
import random
from urllib.parse import quote

import anyio
import httpx

from service.check_store import CHECK_NAME, VALID_CONCLUSIONS, CheckConclusion
from service.github_check import PublishedCheckRun
from service.review_models import ReviewFinding, ReviewReport
from service.scm import PullRequestEvent, raise_for_provider_status

MAX_RESPONSE_BYTES = 1_000_000
MAX_STATUS_ATTEMPTS = 3
STATUS_RETRY_BASE_SECONDS = 0.5
# 409 is GitLab serializing two writers on the same commit; the 5xx and 429
# codes are the transient faults that used to fail an entire review job on the
# first response instead of costing one short backoff.
RETRYABLE_STATUS_CODES = frozenset({409, 429, 500, 502, 503, 504})


def _headers() -> dict[str, str]:
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GITLAB_TOKEN with permission to publish commit statuses is required"
        )
    return {
        "Accept": "application/json",
        "PRIVATE-TOKEN": token,
        "User-Agent": "diffuse-commit-status",
    }


def _status_url(event: PullRequestEvent) -> str:
    project = quote(
        str(event.source_project_id)
        if event.source_project_id > 0
        else event.repo_full_name,
        safe="",
    )
    revision = quote(event.head_sha, safe="")
    return f"{event.api_base_url}/projects/{project}/statuses/{revision}"


async def _post_status(
    client: httpx.AsyncClient,
    event: PullRequestEvent,
    payload: dict[str, object],
) -> dict:
    response: httpx.Response | None = None
    for attempt in range(MAX_STATUS_ATTEMPTS):
        response = await client.post(
            _status_url(event),
            headers=_headers(),
            data=payload,
        )
        if response.status_code not in RETRYABLE_STATUS_CODES:
            break
        if attempt < MAX_STATUS_ATTEMPTS - 1:
            # Jittered so that every review job racing the same commit does not
            # retry on the same instant and reproduce the conflict it backed off
            # from.
            ceiling = STATUS_RETRY_BASE_SECONDS * (2**attempt)
            await anyio.sleep(random.uniform(ceiling / 2, ceiling))
    if response is None:
        raise RuntimeError("GitLab commit-status request was not attempted")
    raise_for_provider_status(response, provider="gitlab")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitLab commit-status response exceeds Diffuse's size limit")
    value = response.json()
    if not isinstance(value, dict) or value.get("id") is None:
        raise RuntimeError("GitLab returned an invalid commit status")
    return value


async def ensure_gitlab_check_run(
    event: PullRequestEvent,
    *,
    external_key: str,
    existing_external_id: str | None = None,
    existing_external_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> PublishedCheckRun:
    if event.provider != "gitlab":
        raise ValueError("GitLab status publisher received a non-GitLab event")
    if existing_external_id:
        return PublishedCheckRun(existing_external_id, existing_external_url)
    description = f"Diffuse review {external_key.rsplit(':', maxsplit=1)[-1]} is running."
    payload = {
        "state": "running",
        "name": CHECK_NAME,
        "ref": event.head_branch,
        "target_url": event.web_url,
        "description": description[:255],
    }

    async def publish(active_client: httpx.AsyncClient) -> PublishedCheckRun:
        value = await _post_status(active_client, event, payload)
        return PublishedCheckRun(
            external_id=str(value["id"]),
            external_url=value.get("target_url") or event.web_url,
        )

    if client is not None:
        return await publish(client)
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await publish(owned_client)


def _completion_description(
    conclusion: CheckConclusion,
    report: ReviewReport | None,
    blocking_severities: tuple[str, ...],
    message: str | None,
    unresolved_findings: tuple[ReviewFinding, ...] | None,
) -> str:
    if report is None:
        return (message or f"Diffuse review completed with {conclusion}.")[:255]
    active_findings = (
        unresolved_findings
        if unresolved_findings is not None
        else tuple(report.findings)
    )
    blocking_count = sum(
        finding.severity.value in blocking_severities
        for finding in active_findings
    )
    return (
        f"Risk {report.risk_score:.1f}/10; {len(active_findings)} active findings; "
        f"{blocking_count} blocking."
    )[:255]


async def complete_gitlab_check_run(
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
    if event.provider != "gitlab":
        raise ValueError("GitLab status publisher received a non-GitLab event")
    if not external_id:
        raise ValueError("GitLab status identity is required")
    if conclusion not in VALID_CONCLUSIONS:
        raise ValueError("Invalid GitLab commit-status conclusion")
    state = {
        "cancelled": "canceled",
        "failure": "failed",
        "neutral": "success",
        "skipped": "skipped",
        "success": "success",
    }[conclusion]
    payload = {
        "state": state,
        "name": CHECK_NAME,
        "ref": event.head_branch,
        "target_url": event.web_url,
        "description": _completion_description(
            conclusion,
            report,
            blocking_severities,
            message,
            unresolved_findings,
        ),
    }

    async def publish(active_client: httpx.AsyncClient) -> None:
        await _post_status(active_client, event, payload)

    if client is not None:
        await publish(client)
        return
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        await publish(owned_client)
