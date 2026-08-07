"""Copy for Diffuse's terminal review-failure notice.

A review job that dies without publishing anything used to be invisible on the
pull request: the status check is opt-in and is created too late to describe an
early failure. This module owns the one comment Diffuse posts instead, so the
GitHub publisher stays thin and the wording, identity marker, and redaction
rules live in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
RETRIES_EXHAUSTED_CODE = "review_workflow_exhausted"
NON_RETRYABLE_CODE = "review_workflow_failed"
AGENT_AUTH_REQUIRED_CODE = "agent_auth_required"
RETRIES_EXHAUSTED_SUMMARY = (
    "Diffuse could not complete this review after exhausting its retry policy."
)
NON_RETRYABLE_SUMMARY = (
    "Diffuse could not complete this review because the job failed in a way "
    "that retrying cannot resolve."
)
MAX_NOTICE_BODY_CHARS = 4_000
REDACTION_PLACEHOLDER = "[redacted]"
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Credentials embedded in a connection string or clone URL.
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@"),
    # Provider access tokens. The glpat- rule is kept deliberately: redaction is
    # defence in depth, and an operator migrating off GitLab may still have a
    # stale GITLAB_TOKEN in the environment when a failure is rendered.
    # GitHub's 2026 stateless installation tokens are ~520-character
    # `ghs_`-prefixed JWTs with dots, hyphens, and additional underscores.
    # The wider character class also keeps covering the classic opaque forms.
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9.\-_]{16,}"),
    re.compile(r"(?i)\bglpat-[A-Za-z0-9_\-]{16,}"),
    # Anything self-identifying as a secret, including a scheme-prefixed value.
    re.compile(
        r"(?i)\b(?:api[_\-]?key|access[_\-]?token|auth(?:orization)?|bearer"
        r"|client[_\-]?secret|password|passwd|private[_\-]?key|secret|token)\b"
        r"\s*[:=]\s*(?:bearer|basic|token)?\s*\S+"
    ),
)


def redact_credentials(text: str) -> str:
    """Blank out credential-shaped substrings before they reach a pull request."""
    redacted = text
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(REDACTION_PLACEHOLDER, redacted)
    return redacted


@dataclass(frozen=True)
class TerminalReviewFailure:
    """One review job that will never produce a review."""

    job_id: int
    error_code: str
    summary: str

    def __post_init__(self) -> None:
        if self.job_id <= 0:
            raise ValueError("Terminal review failure job id must be positive")
        if not ERROR_CODE_PATTERN.fullmatch(self.error_code):
            raise ValueError("Terminal review failure code is invalid")
        if not self.summary.strip():
            raise ValueError("Terminal review failure summary must not be empty")

    @property
    def marker(self) -> str:
        """The Diffuse-owned identity marker that makes the notice idempotent.

        Deliberately does not include ``job_id``. The marker scopes one notice
        per pull request, so a repeated failure edits that notice instead of
        appending another comment. Keying it per job meant every retry, and every
        later failing job on the same pull request, posted a fresh comment onto a
        pull request that was already failing. The job id is still rendered in
        the visible body, where support needs it.

        The ``diffuse-`` prefix is also what ``is_diffuse_generated`` matches,
        which is what keeps the notice out of Diffuse's own feedback and
        conversation ingestion.
        """
        return "<!-- diffuse-review-failure -->"


def terminal_review_failure(
    job_id: int,
    *,
    retries_exhausted: bool,
) -> TerminalReviewFailure:
    if retries_exhausted:
        return TerminalReviewFailure(
            job_id=job_id,
            error_code=RETRIES_EXHAUSTED_CODE,
            summary=RETRIES_EXHAUSTED_SUMMARY,
        )
    return TerminalReviewFailure(
        job_id=job_id,
        error_code=NON_RETRYABLE_CODE,
        summary=NON_RETRYABLE_SUMMARY,
    )


def agent_auth_required_failure(job_id: int, runtime: str) -> TerminalReviewFailure:
    """A safe, idempotent PR notice for a runner whose vendor login expired."""

    if runtime not in {"claude", "codex"}:
        raise ValueError("agent auth failure runtime is invalid")
    reconnect = (
        f"docker compose --profile agent-{runtime} run --rm "
        f"agent-runner-{runtime} agent login {runtime}"
    )
    return TerminalReviewFailure(
        job_id=job_id,
        error_code=AGENT_AUTH_REQUIRED_CODE,
        summary=(
            f"Diffuse could not authenticate the {runtime} review runner. "
            f"An operator must reconnect it with `{reconnect}` and explicitly re-run this review."
        ),
    )


def format_failure_notice(failure: TerminalReviewFailure) -> str:
    """Render the pull-request comment for a terminal review failure.

    Every ingredient is either a validated slug, an integer, or fixed copy, so
    no exception text, traceback, or credential can reach the comment. The
    redaction pass is a second line of defence rather than the first.
    """
    body = "\n\n".join(
        (
            failure.marker,
            "## Diffuse could not complete this review",
            (
                f"{failure.summary} No findings were published, so these "
                "changes have **not** been reviewed."
            ),
            (
                f"**Error code:** `{failure.error_code}` · "
                f"**Job id:** `{failure.job_id}`"
            ),
            (
                "Send the error code and job id to whoever operates this "
                "Diffuse instance — the worker logs record the underlying "
                "error against that job id. Pushing a new commit queues a "
                "fresh review."
            ),
        )
    )
    return redact_credentials(body)[:MAX_NOTICE_BODY_CHARS]
