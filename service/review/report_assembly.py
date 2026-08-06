"""The parts of a review that are contract rather than runtime.

Severity ordering, the risk floor, the confidence score, finding identity, the
presentation flags, and the thresholds a finding must clear are properties of a
`ReviewReport` — not of the way one was produced. They lived in
`service/review/engine.py` because the one-shot API runtime was the only
producer, which meant a second runtime could not exist without either
duplicating them or inheriting that whole module.

Both runtimes call into here, so a finding is filtered, fingerprinted, and
scored identically no matter which one investigated. `MIN_REVIEW_CONFIDENCE`
therefore keeps its exact meaning when the one-shot runtime is retired: Diffuse
filters an agent's self-reported confidence the same way it filters a verifier's
today.

Deliberately free of model, provider, and LiteLLM imports. That is what makes it
survive `service/review/engine.py` being deleted, and it is worth keeping true.
"""

from __future__ import annotations

import hashlib
import os

from repository_policy.resolve import ResolvedReviewPolicy
from service.diff_parser import ParsedDiff
from service.models.review import CandidateFinding, ReviewFinding, Severity

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
        "fix_with_agent_enabled": policy.fix_with_agent_enabled,
        "diagram_collapsible": policy.diagram_collapsible,
        "diagram_default_open": policy.diagram_default_open,
    }
