"""The parts of a review that are contract rather than runtime.

Severity ordering, the risk floor, the confidence score, finding identity, the
presentation flags, and the thresholds a finding must clear are properties of a
`ReviewReport` — not of the way one was produced. They live outside the Agent
execution boundary so every Review Agent shares the same publication rules.

Every Review Agent calls into here, so a finding is filtered, fingerprinted,
and scored identically no matter which one investigated.

Deliberately free of model and provider imports. That is what makes it
survive `diffuse/review/engine.py` being deleted, and it is worth keeping true.
"""

from __future__ import annotations

import hashlib
import os

from diffuse_protocol.review import (
    CandidateFinding,
    Category,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
    Severity,
    VerificationDecision,
)

from diffuse.repository.policy.resolve import ResolvedReviewPolicy
from diffuse.review.diff import ParsedDiff

#: Presentation order. Not the same as the risk floor below: this decides what a
#: reader sees first, that decides what the review is allowed to score.
SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}

#: The lowest risk score a review carrying a finding of each severity may report.
#: A CRITICAL finding cannot appear on a review that calls itself low risk.
SEVERITY_RISK_FLOOR = {
    Severity.CRITICAL: 9.0,
    Severity.HIGH: 7.0,
    Severity.MEDIUM: 4.0,
    Severity.LOW: 2.0,
}

MIN_DIAGRAM_CHANGED_LINES = 40
MIN_MULTI_FILE_DIAGRAM_CHANGED_LINES = 12

#: The most findings any single review publishes, whatever the runtime produced.
#: A review nobody reads because it is too long is a review that did not happen.
MAX_PUBLISHED_FINDINGS = 25


def reviewable_diff(
    parsed_diff: ParsedDiff,
    policy: ResolvedReviewPolicy | None,
) -> tuple[ParsedDiff, int]:
    """Return the files this review may inspect and the number policy excluded."""

    if policy is None:
        return parsed_diff, 0
    selected = ParsedDiff(
        files=tuple(
            file
            for file in parsed_diff.files
            if file.comment_path and policy.allows_path(file.comment_path)
        )
    )
    return selected, len(parsed_diff.files) - len(selected.files)


def all_files_disabled_report(
    *,
    diff_file_count: int,
    ignored_file_count: int,
    policy: ResolvedReviewPolicy | None,
) -> ReviewReport:
    """The policy-owned result when no changed file may be reviewed."""

    return ReviewReport(
        summary="Review disabled by repository policy for all changed files.",
        risk_score=0,
        confidence_score=0,
        findings=[],
        diff_file_count=diff_file_count,
        reviewed_file_count=0,
        ignored_file_count=ignored_file_count,
        inline_comments_enabled=False,
        publication_enabled=False,
        skip_reason="all_files_disabled",
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        # Reported, not merely absent: policy stopped this review before any
        # model call, so zero verifier spend is a measurement rather than a gap.
        verifier_prompt_tokens=0,
        verifier_completion_tokens=0,
        **review_presentation(policy),
    )


def minimum_review_confidence() -> float:
    value = float(os.environ.get("MIN_REVIEW_CONFIDENCE", "0.75"))
    if not 0 <= value <= 1:
        raise ValueError("MIN_REVIEW_CONFIDENCE must be between 0 and 1")
    return value


def fingerprint(candidate: CandidateFinding, title: str) -> str:
    """Stable identity for a finding, used to carry it across review runs.

    Part of the contract rather than the runtime: finding continuity has to hold
    when the same defect is found by a different runtime on a later push, so the
    identity cannot include anything about how it was found.
    """

    identity = "\0".join(
        (
            candidate.file_path,
            candidate.side,
            str(candidate.line),
            candidate.category.value,
            (
                candidate.security_classification.value
                if candidate.security_classification is not None
                else "none"
            ),
            title.casefold(),
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _normalize_security_candidate(
    candidate: CandidateFinding,
    policy: ResolvedReviewPolicy | None,
) -> CandidateFinding | None:
    classification = candidate.security_classification
    if candidate.category is not Category.SECURITY:
        return candidate if classification is None else None
    if classification is None:
        candidate = candidate.model_copy(
            update={"security_classification": SecurityClassification.VULNERABILITY}
        )
        classification = SecurityClassification.VULNERABILITY
    if (
        classification is SecurityClassification.PREVENTATIVE
        and (
            policy is None
            or not policy.allows_preventative_security(candidate.file_path)
            or candidate.severity in {Severity.CRITICAL, Severity.HIGH}
        )
    ):
        return None
    return candidate


def deduplicate_candidates_with_ids(
    candidates: list[CandidateFinding],
    parsed_diff: ParsedDiff,
    policy: ResolvedReviewPolicy | None = None,
) -> tuple[list[CandidateFinding], dict[str, str]]:
    """Keep only publishable candidates and map raw ids to filtered ids."""

    selected: dict[tuple[str, str, int, str, str], tuple[str, CandidateFinding]] = {}
    for index, raw_candidate in enumerate(candidates):
        raw_id = f"candidate-{index}"
        candidate = _normalize_security_candidate(raw_candidate, policy)
        if candidate is None:
            continue
        if policy is not None and not policy.allows_path(candidate.file_path):
            continue
        if not parsed_diff.is_commentable(
            candidate.file_path,
            candidate.side,
            candidate.line,
        ):
            continue
        key = (
            candidate.file_path,
            candidate.side,
            candidate.line,
            candidate.category.value,
            (
                candidate.security_classification.value
                if candidate.security_classification is not None
                else ""
            ),
        )
        existing = selected.get(key)
        if existing is None or candidate.confidence > existing[1].confidence:
            selected[key] = (raw_id, candidate)
    ordered = sorted(
        selected.values(),
        key=lambda item: (
            SEVERITY_ORDER[item[1].severity],
            -item[1].confidence,
            item[1].file_path,
            item[1].line,
        ),
    )[:80]
    filtered = [candidate for _raw_id, candidate in ordered]
    id_map = {
        raw_id: f"candidate-{index}"
        for index, (raw_id, _candidate) in enumerate(ordered)
    }
    return filtered, id_map


def deduplicate_candidates(
    candidates: list[CandidateFinding],
    parsed_diff: ParsedDiff,
    policy: ResolvedReviewPolicy | None = None,
) -> list[CandidateFinding]:
    """Keep only policy-allowed candidates anchored to changed lines.

    This is a report invariant: an Agent must
    not publish a finding whose claimed location is outside the supplied diff.
    """

    filtered, _id_map = deduplicate_candidates_with_ids(candidates, parsed_diff, policy)
    return filtered


def verified_findings(
    candidates: list[CandidateFinding],
    decisions: dict[str, VerificationDecision],
    duplicate_decisions: set[str],
    policy: ResolvedReviewPolicy | None,
) -> list[ReviewFinding]:
    """Apply the shared confidence, severity, and publication limits."""

    findings: list[ReviewFinding] = []
    for index, candidate in enumerate(candidates):
        candidate_id = f"candidate-{index}"
        decision = decisions.get(candidate_id)
        if (
            policy is not None
            and candidate.security_classification is SecurityClassification.PREVENTATIVE
        ):
            threshold = policy.preventative_security_threshold_for(candidate.file_path)
        else:
            threshold = (
                policy.threshold_for(candidate.file_path)
                if policy is not None
                else minimum_review_confidence()
            )
        if (
            decision is None
            or candidate_id in duplicate_decisions
            or not decision.keep
            or min(candidate.confidence, decision.confidence) < threshold
        ):
            continue
        title = decision.revised_title or candidate.title
        body = decision.revised_body or candidate.body
        severity = decision.revised_severity or candidate.severity
        if (
            candidate.security_classification is SecurityClassification.PREVENTATIVE
            and severity in {Severity.CRITICAL, Severity.HIGH}
        ):
            continue
        if policy is not None and not policy.allows_severity(candidate.file_path, severity.value):
            continue
        suggested_fix = (
            decision.revised_suggested_fix
            if decision.revised_suggested_fix is not None
            else candidate.suggested_fix
        )
        findings.append(
            ReviewFinding(
                fingerprint=fingerprint(candidate, title),
                title=title,
                body=body,
                severity=severity,
                category=candidate.category,
                security_classification=candidate.security_classification,
                confidence=min(candidate.confidence, decision.confidence),
                file_path=candidate.file_path,
                line=candidate.line,
                side=candidate.side,
                evidence=candidate.evidence,
                suggested_fix=suggested_fix,
            )
        )
        if len(findings) == MAX_PUBLISHED_FINDINGS:
            break
    return sorted(
        findings,
        key=lambda finding: (
            SEVERITY_ORDER[finding.severity],
            -finding.confidence,
            finding.file_path,
            finding.line,
        ),
    )


def risk_floor(findings: list[ReviewFinding]) -> float:
    return max(
        (SEVERITY_RISK_FLOOR[finding.severity] for finding in findings),
        default=0.0,
    )


def diagram_would_help(parsed_diff: ParsedDiff) -> bool:
    changed_lines = sum(
        entry.marker in {"+", "-"}
        for file in parsed_diff.files
        for entry in file.entries
    )
    return changed_lines >= MIN_DIAGRAM_CHANGED_LINES or (
        len(parsed_diff.files) >= 2
        and changed_lines >= MIN_MULTI_FILE_DIAGRAM_CHANGED_LINES
    )


def review_confidence_score(
    *,
    risk_score: float,
    finding_count: int,
    diff_file_count: int,
    reviewed_file_count: int,
    ignored_file_count: int,
) -> int:
    """Map verified review evidence to an explainable 0-5 readiness score."""
    if not 0 <= risk_score <= 10:
        raise ValueError("Review risk score must be between 0 and 10")
    if min(
        finding_count,
        diff_file_count,
        reviewed_file_count,
        ignored_file_count,
    ) < 0:
        raise ValueError("Review confidence inputs cannot be negative")
    if risk_score == 0:
        score = 5
    elif risk_score <= 2.5:
        score = 4
    elif risk_score <= 5:
        score = 3
    elif risk_score <= 7.5:
        score = 2
    elif risk_score < 10:
        score = 1
    else:
        score = 0
    if finding_count >= 10:
        score = min(score, 1)
    elif finding_count >= 6:
        score = min(score, 2)
    elif finding_count >= 3:
        score = min(score, 3)
    if diff_file_count and reviewed_file_count + ignored_file_count < diff_file_count:
        score = min(score, 2)
    if ignored_file_count:
        score = min(score, 4)
    if diff_file_count and reviewed_file_count == 0:
        score = 0
    return score


def review_presentation(
    policy: ResolvedReviewPolicy | None,
) -> dict[str, bool]:
    if policy is None:
        return {}
    summary = policy.summary_section
    issues = policy.issues_table_section
    confidence = policy.confidence_score_section
    return {
        "summary_section_included": summary.included,
        "summary_section_collapsible": summary.collapsible,
        "summary_section_default_open": summary.default_open,
        "issues_table_section_included": issues.included,
        "issues_table_section_collapsible": issues.collapsible,
        "issues_table_section_default_open": issues.default_open,
        "confidence_score_section_included": confidence.included,
        "confidence_score_section_collapsible": confidence.collapsible,
        "confidence_score_section_default_open": confidence.default_open,
        "footer_included": policy.footer_included,
        "update_description": policy.update_description,
        "summary_comment_enabled": policy.summary_comment_enabled,
        "fix_with_agent_enabled": False,
        "diagram_collapsible": policy.diagram_collapsible,
        "diagram_default_open": policy.diagram_default_open,
    }
