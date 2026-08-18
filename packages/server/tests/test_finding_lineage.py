import hashlib

from diffuse.review.lineage import HistoricalFinding, classify_finding_lineage
from diffuse_protocol.review import (
    Category,
    ReviewFinding,
    SecurityClassification,
    Severity,
)


def _finding(
    identity: str,
    *,
    title: str = "Validate the authorization boundary",
    body: str = "The handler accepts a tenant ID without checking ownership.",
    path: str = "service/api.py",
    line: int = 20,
    category: Category = Category.SECURITY,
) -> ReviewFinding:
    return ReviewFinding(
        fingerprint=hashlib.sha256(identity.encode()).hexdigest(),
        title=title,
        body=body,
        severity=Severity.HIGH,
        category=category,
        confidence=0.91,
        file_path=path,
        line=line,
        side="RIGHT",
        evidence="The changed call forwards the unscoped tenant ID.",
        suggested_fix="Verify tenant ownership before forwarding.",
    )


def test_first_review_creates_new_lineages():
    findings = (_finding("first"), _finding("second", path="service/jobs.py"))

    transitions = classify_finding_lineage(
        findings,
        (),
        touched_paths=frozenset(),
    )

    assert [(item.kind, item.lineage_id) for item in transitions] == [
        ("new", None),
        ("new", None),
    ]


def test_reworded_and_shifted_finding_stays_in_the_same_lineage():
    previous = _finding("previous", line=20)
    current = _finding(
        "current",
        title="Validate authorization at the tenant boundary",
        body="The handler accepts a tenant ID without validating its ownership.",
        line=47,
    )

    transitions = classify_finding_lineage(
        (current,),
        (HistoricalFinding(7, "active", previous),),
        touched_paths=frozenset({"service/api.py"}),
    )

    assert len(transitions) == 1
    assert transitions[0].kind == "persistent"
    assert transitions[0].lineage_id == 7


def test_active_finding_is_addressed_only_when_its_file_was_touched():
    previous = _finding("previous")

    untouched = classify_finding_lineage(
        (),
        (HistoricalFinding(7, "active", previous),),
        touched_paths=frozenset({"service/other.py"}),
    )
    touched = classify_finding_lineage(
        (),
        (HistoricalFinding(7, "active", previous),),
        touched_paths=frozenset({"service/api.py"}),
    )

    assert untouched == ()
    assert len(touched) == 1
    assert touched[0].kind == "addressed"
    assert touched[0].lineage_id == 7
    assert touched[0].finding is None


def test_addressed_finding_reopens_when_detected_again():
    previous = _finding("same")
    current = previous.model_copy(update={"line": 24})

    transitions = classify_finding_lineage(
        (current,),
        (HistoricalFinding(9, "addressed", previous),),
        touched_paths=frozenset({"service/api.py"}),
    )

    assert len(transitions) == 1
    assert transitions[0].kind == "reopened"
    assert transitions[0].lineage_id == 9


def test_findings_never_match_across_paths_or_categories():
    previous = _finding("previous")
    moved_file = _finding("moved", path="service/admin.py")
    changed_category = _finding(
        "category",
        category=Category.CORRECTNESS,
    )

    transitions = classify_finding_lineage(
        (moved_file, changed_category),
        (HistoricalFinding(7, "active", previous),),
        touched_paths=frozenset(),
    )

    assert [item.kind for item in transitions] == ["new", "new"]


def test_renamed_path_aliases_preserve_finding_lineage():
    previous = _finding("previous", path="service/old.py", line=20)
    current = _finding(
        "current",
        title="Validate authorization at the tenant boundary",
        body="The handler accepts a tenant ID without validating its ownership.",
        path="service/new.py",
        line=24,
    )

    transitions = classify_finding_lineage(
        (current,),
        (HistoricalFinding(7, "active", previous),),
        touched_paths=frozenset({"service/new.py"}),
        path_aliases={"service/old.py": "service/new.py"},
    )

    assert len(transitions) == 1
    assert transitions[0].kind == "persistent"
    assert transitions[0].lineage_id == 7


def test_findings_never_match_across_security_classifications():
    vulnerability = _finding("same-explicit-fingerprint")
    preventative = vulnerability.model_copy(
        update={
            "severity": Severity.MEDIUM,
            "security_classification": SecurityClassification.PREVENTATIVE,
        }
    )

    transitions = classify_finding_lineage(
        (preventative,),
        (HistoricalFinding(7, "active", vulnerability),),
        touched_paths=frozenset(),
    )

    assert len(transitions) == 1
    assert transitions[0].kind == "new"
    assert transitions[0].lineage_id is None
