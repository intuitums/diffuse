"""Tests for review invariants shared by every runtime."""

import pytest

from repository_policy.models import PolicyLayer, RepositoryConfig, RepositoryPolicySnapshot
from repository_policy.resolve import resolve_review_policy
from service.diff_parser import parse_unified_diff
from service.models.review import (
    CandidateFinding,
    Category,
    ReviewReport,
    SecurityClassification,
    Severity,
    VerificationDecision,
)
from service.review import report_assembly

DIFF = """\\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = False
+new = True
"""


def _candidate(**changes) -> CandidateFinding:
    values = {
        "title": "Validate the authorization boundary",
        "body": "The changed branch bypasses the existing authorization check.",
        "severity": Severity.HIGH,
        "category": Category.SECURITY,
        "confidence": 0.99,
        "file_path": "app.py",
        "line": 1,
        "side": "RIGHT",
        "evidence": "new = True replaces the authorization guard.",
    }
    values.update(changes)
    return CandidateFinding(**values)


def _decision(**changes) -> VerificationDecision:
    values = {
        "candidate_id": "candidate-0",
        "keep": True,
        "confidence": 0.98,
        "rationale": "The changed line directly demonstrates the bypass.",
    }
    values.update(changes)
    return VerificationDecision(**values)


def test_deduplicate_candidates_requires_a_changed_line():
    candidates = [_candidate(line=2), _candidate(line=1)]

    assert report_assembly.deduplicate_candidates(candidates, parse_unified_diff(DIFF)) == [
        candidates[1]
    ]


def test_verified_findings_applies_the_default_confidence_floor(monkeypatch):
    monkeypatch.setenv("MIN_REVIEW_CONFIDENCE", "0.95")
    candidate = _candidate(confidence=0.96)

    findings = report_assembly.verified_findings(
        [candidate],
        {"candidate-0": _decision(confidence=0.94)},
        set(),
        None,
    )

    assert findings == []


def test_verified_findings_rejects_preventative_high_severity_after_revision():
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {"version": 1, "security": {"preventative": True}}
                    ),
                ),
            )
        ),
        {"app.py"},
    )
    candidate = _candidate(
        severity=Severity.MEDIUM,
        security_classification=SecurityClassification.PREVENTATIVE,
    )

    findings = report_assembly.verified_findings(
        [candidate],
        {"candidate-0": _decision(revised_severity=Severity.HIGH)},
        set(),
        policy,
    )

    assert findings == []


def test_all_files_disabled_report_cannot_publish():
    report = report_assembly.all_files_disabled_report(
        diff_file_count=2,
        ignored_file_count=2,
        policy=None,
    )

    assert report.skip_reason == "all_files_disabled"
    assert not report.publication_enabled
    assert not report.inline_comments_enabled


def _report(**changes) -> ReviewReport:
    values = {
        "summary": "none",
        "risk_score": 0,
        "findings": [],
        "diff_file_count": 0,
        "reviewed_file_count": 0,
        "context_chunk_count": 0,
        "prompt_tokens": 3,
        "completion_tokens": 2,
    }
    values.update(changes)
    return ReviewReport(**values)


def test_review_report_rejects_a_verifier_usage_larger_than_its_total():
    with pytest.raises(ValueError, match="cannot exceed total"):
        _report(verifier_prompt_tokens=4, verifier_completion_tokens=0)


def test_review_report_rejects_a_half_reported_split():
    """Half a split cannot be priced and must not look like a whole one."""

    with pytest.raises(ValueError, match="both be reported or both be absent"):
        _report(verifier_prompt_tokens=1)


def test_an_unreported_split_is_not_a_zero_split():
    """Zero says the verifier was free; absent says nobody measured it.

    Collapsing the two would let a runtime that reports no split be priced
    entirely on the candidate's rate card, which for a cross-family pair is a
    different number presented as the same one.
    """

    assert not _report().reports_verifier_usage
    assert _report(
        verifier_prompt_tokens=0,
        verifier_completion_tokens=0,
    ).reports_verifier_usage


def test_a_policy_skipped_review_reports_a_measured_zero():
    """No model call ran, so the split is known, not missing."""

    report = report_assembly.all_files_disabled_report(
        diff_file_count=2,
        ignored_file_count=2,
        policy=None,
    )

    assert report.reports_verifier_usage
    assert report.verifier_prompt_tokens == 0
