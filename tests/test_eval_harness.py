"""Plumbing tests for the fixture harness and its baseline gate.

These verify that the harness assembles the `observed` structure
`service/evaluation.py` consumes, that it refuses fixtures whose labels the
review engine could never satisfy, and that the baseline comparison actually
fails on a worse run. They do NOT measure review quality: `_call_structured` is
stubbed here exactly as it is in `tests/test_review_engine.py`, so the findings
are whatever the stub returns.

That distinction is the point of this unit. 774 tests passed while the product
could not complete a single review, because every review test stubbed the model
call. `run` -- the one command that reaches a provider -- is unreachable from a
machine with no credential, so its behaviour against a live model is asserted
by `evals/CAPTURE.md`, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import eval_harness
from service.diff_parser import parse_unified_diff
from service.evaluation import (
    EvaluationCase,
    EvaluationSuite,
    ModelPricing,
    ObservedFinding,
    RunConfiguration,
    score_evaluation,
)
from service.models.review import (
    CandidateBatch,
    CandidateFinding,
    Category,
    SecurityClassification,
    Severity,
    VerificationBatch,
    VerificationDecision,
)
from service.review import engine as review_engine

FIXTURE_ROOT = Path("evals/fixtures")

DIFF = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-return trusted_value
+return user_value
 keep_running()
"""


def _write_fixture(
    root: Path,
    case_id: str,
    *,
    diff: str = DIFF,
    expected: list[dict] | None = None,
    contexts: list[dict] | None = None,
    case_id_in_json: str | None = None,
) -> Path:
    directory = root / case_id
    (directory / "context").mkdir(parents=True, exist_ok=True)
    (directory / "diff.patch").write_text(diff)
    (directory / "context" / "helper.py").write_text("def helper():\n    return 1\n")
    payload = {
        "schema_version": "diffuse-eval-fixture-v2",
        "case_id": case_id_in_json or case_id,
        "description": "A synthetic fixture used only to exercise the harness.",
        "expected": expected
        if expected is not None
        else [
            {
                "finding_id": f"{case_id}-1",
                "title": "Authorization bypass",
                "file_path": "app.py",
                "line": 1,
                "side": "RIGHT",
                "category": "security",
                "line_tolerance": 3,
            }
        ],
        "addressed_finding_ids": [],
        "contexts": contexts
        if contexts is not None
        else [
            {
                "path": "context/helper.py",
                "file_path": "lib/helper.py",
                "symbol_name": "helper",
                "retrieval_reason": "graph_callee",
                "relevance_score": 0.5,
            }
        ],
    }
    (directory / "case.json").write_text(json.dumps(payload))
    return directory


def _stub_call(monkeypatch, *, findings: list[CandidateFinding], keep: set[str]):
    """Stand in for the model, and record which stage each call came from."""

    seen: list[str] = []

    def fake_call(response_model, **kwargs):
        if response_model is CandidateBatch:
            seen.append("candidate")
            return (
                CandidateBatch(
                    analysis_summary="Candidates were considered.",
                    findings=findings,
                ),
                11,
                5,
            )
        if response_model is VerificationBatch:
            seen.append("verifier")
            return (
                VerificationBatch(
                    summary="The change introduces an authorization bypass.",
                    risk_score=6,
                    decisions=[
                        VerificationDecision(
                            candidate_id=candidate_id,
                            keep=True,
                            confidence=0.95,
                            rationale="The changed line returns untrusted data.",
                        )
                        for candidate_id in sorted(keep)
                    ],
                ),
                7,
                3,
            )
        seen.append("diagram")
        raise AssertionError("unexpected structured call")

    monkeypatch.setattr(review_engine, "_call_structured", fake_call)
    return seen


def _candidate(line: int = 1, confidence: float = 0.95) -> CandidateFinding:
    return CandidateFinding(
        title="Authorization bypass",
        body="Untrusted data now crosses the authorization boundary.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        security_classification=SecurityClassification.VULNERABILITY,
        confidence=confidence,
        file_path="app.py",
        line=line,
        side="RIGHT",
        evidence="The changed return uses user_value without validation.",
        suggested_fix="Validate user_value before returning it.",
    )


# ---------------------------------------------------------------------------
# fixture loading and validation
# ---------------------------------------------------------------------------


def test_committed_fixtures_all_load_and_validate():
    fixtures = eval_harness.load_fixtures(FIXTURE_ROOT)
    # Pinned, not a floor. A baseline is captured against a fixture set, and a
    # `>= 5` assertion stays green while three of them quietly stop loading.
    assert [loaded.fixture.case_id for loaded in fixtures] == [
        "clean-settings-refactor",
        "invite-redemption-race",
        "missing-null-check",
        "n-plus-one-rollup",
        "off-by-one-page-slice",
        "path-traversal-attachment",
        "sql-injection-sort-column",
        "unhandled-error-path",
    ]
    for loaded in fixtures:
        assert loaded.diff_text.startswith("diff --git ")
        assert loaded.contexts, f"{loaded.fixture.case_id} has no retrieved context"
        assert loaded.digest


def test_a_directory_without_a_case_file_is_an_error_not_a_skip(tmp_path):
    """Silently skipping one would shrink the suite with nothing red.

    Renaming `case.json` to `case.jsonc` used to drop that fixture from every
    run while every test stayed green -- and the fixture set is what a baseline is
    captured from.
    """

    _write_fixture(tmp_path, "present")
    (tmp_path / "absent").mkdir()

    with pytest.raises(eval_harness.FixtureError, match="missing case.json"):
        eval_harness.load_fixtures(tmp_path)


def test_every_committed_label_claims_at_most_a_couple_of_lines():
    """Even with title overlap, a wider span creates more collision surface.

    Every committed label sits exactly on a changed line, so none of them needs
    more than one line of slack.
    """

    for loaded in eval_harness.load_fixtures(FIXTURE_ROOT):
        for expected in loaded.fixture.expected:
            assert expected.line_tolerance <= 1, (
                f"{loaded.fixture.case_id}/{expected.finding_id} claims "
                f"{2 * expected.line_tolerance + 1} lines"
            )


def test_a_fixture_digest_changes_when_the_diff_changes(tmp_path):
    directory = _write_fixture(tmp_path, "hashed")
    before = eval_harness.load_fixture(directory).digest

    (directory / "diff.patch").write_text(DIFF.replace("user_value", "other_value"))

    assert eval_harness.load_fixture(directory).digest != before


def test_committed_fixtures_include_a_case_with_no_expected_findings():
    """Precision is unmeasurable without a clean change to get wrong."""

    fixtures = eval_harness.load_fixtures(FIXTURE_ROOT)
    assert any(not loaded.fixture.expected for loaded in fixtures)


def test_committed_fixtures_cover_more_than_one_category():
    categories = {
        expected.category
        for loaded in eval_harness.load_fixtures(FIXTURE_ROOT)
        for expected in loaded.fixture.expected
    }
    assert len(categories) >= 3


def test_fixture_labeling_a_line_the_diff_does_not_change_is_refused(tmp_path):
    """The defect in evals/baseline.example.json, caught at load time.

    That file is a hand-written example suite for `diffuse evaluate`, not a
    captured baseline. It labels `service/webhook.py:42` and ships no diff, so
    its recorded run scores 0% recall no matter how good the review engine is.
    """

    _write_fixture(
        tmp_path,
        "unreachable-label",
        expected=[
                {
                    "finding_id": "unreachable-1",
                    "title": "Unreachable defect",
                    "file_path": "app.py",
                    "line": 42,
                    "side": "RIGHT",
                "category": "security",
                "line_tolerance": 3,
            }
        ],
    )
    with pytest.raises(eval_harness.FixtureError, match="not within 3 lines"):
        eval_harness.load_fixtures(tmp_path)


def test_fixture_labeling_a_file_the_diff_does_not_touch_is_refused(tmp_path):
    _write_fixture(
        tmp_path,
        "wrong-file",
        expected=[
                {
                    "finding_id": "wrong-file-1",
                    "title": "Wrong-file defect",
                    "file_path": "service/webhook.py",
                    "line": 1,
                    "side": "RIGHT",
                "category": "security",
                "line_tolerance": 3,
            }
        ],
    )
    with pytest.raises(eval_harness.FixtureError, match="which the diff does not change"):
        eval_harness.load_fixtures(tmp_path)


def test_fixture_labeling_the_wrong_diff_side_is_refused(tmp_path):
    addition_only = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1 @@
+unsafe_call()
"""
    _write_fixture(
        tmp_path,
        "wrong-side",
        diff=addition_only,
        expected=[
            {
                "finding_id": "wrong-side-1",
                "title": "Unsafe call lacks validation",
                "file_path": "app.py",
                "line": 1,
                "side": "LEFT",
                "category": "security",
                "line_tolerance": 0,
            }
        ],
    )

    with pytest.raises(eval_harness.FixtureError, match=r"app.py:1 \(LEFT\)"):
        eval_harness.load_fixtures(tmp_path)


def test_case_id_must_match_the_directory_name(tmp_path):
    _write_fixture(tmp_path, "on-disk", case_id_in_json="in-json")
    with pytest.raises(eval_harness.FixtureError, match="declares case_id"):
        eval_harness.load_fixtures(tmp_path)


def test_fixture_paths_cannot_escape_the_fixture_directory(tmp_path):
    directory = _write_fixture(tmp_path, "escaping")
    payload = json.loads((directory / "case.json").read_text())
    payload["diff_path"] = "../../etc/passwd"
    (directory / "case.json").write_text(json.dumps(payload))
    with pytest.raises(eval_harness.FixtureError, match="must stay inside the fixture"):
        eval_harness.load_fixtures(tmp_path)


def test_fixture_directory_with_no_fixtures_is_refused(tmp_path):
    with pytest.raises(eval_harness.FixtureError, match="no fixtures under"):
        eval_harness.load_fixtures(tmp_path)


def test_unknown_fixture_field_is_refused(tmp_path):
    directory = _write_fixture(tmp_path, "extra-field")
    payload = json.loads((directory / "case.json").read_text())
    payload["expected_recall"] = 0.9
    (directory / "case.json").write_text(json.dumps(payload))
    with pytest.raises(eval_harness.FixtureError, match="invalid fixture"):
        eval_harness.load_fixtures(tmp_path)


# ---------------------------------------------------------------------------
# driving the review engine
# ---------------------------------------------------------------------------


def test_run_fixture_emits_the_observed_structure_the_scorer_consumes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")
    seen = _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "one-finding")
    loaded = eval_harness.load_fixtures(tmp_path)[0]

    case = eval_harness.run_fixture(
        loaded,
        candidate_model="openai/gpt-4.1-mini",
        verifier_model="openai/gpt-4.1-mini",
    )

    assert seen == ["candidate", "verifier"]
    assert isinstance(case, EvaluationCase)
    assert case.case_id == "one-finding"
    assert case.observed == [
        ObservedFinding(
            title="Authorization bypass",
            file_path="app.py",
            line=1,
            side="RIGHT",
            category=Category.SECURITY,
            severity=Severity.HIGH,
            fingerprint=case.observed[0].fingerprint,
        )
    ]
    assert case.observed[0].fingerprint is not None
    # The scorer accepts it without any transcription step.
    score = score_evaluation(
        EvaluationSuite(
            schema_version="diffuse-evaluation-v3",
            name="plumbing",
            model="openai/gpt-4.1-mini",
            verifier_model="openai/gpt-4.1-mini",
            cases=[case],
        )
    )
    assert score.true_positives == 1
    assert score.false_positives == 0


def test_run_fixture_records_candidate_and_verifier_tokens_separately(
    tmp_path, monkeypatch
):
    """`ReviewReport` folds the two together; the suite prices them apart."""

    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security,correctness")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "token-split")
    loaded = eval_harness.load_fixtures(tmp_path)[0]

    case = eval_harness.run_fixture(
        loaded,
        candidate_model="openai/gpt-4.1-mini",
        verifier_model="anthropic/claude-haiku-4-5",
    )

    # Two candidate passes at (11, 5), one verification at (7, 3).
    assert (case.prompt_tokens, case.completion_tokens) == (22, 10)
    assert (case.verifier_prompt_tokens, case.verifier_completion_tokens) == (7, 3)


def test_the_token_recorder_restores_the_real_call_path(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "restores")
    loaded = eval_harness.load_fixtures(tmp_path)[0]
    before = review_engine._call_structured

    eval_harness.run_fixture(
        loaded,
        candidate_model="openai/gpt-4.1-mini",
        verifier_model="openai/gpt-4.1-mini",
    )

    assert review_engine._call_structured is before


def test_run_fixture_restores_the_call_path_when_the_review_raises(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")

    def exploding(*_args, **_kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(review_engine, "_call_structured", exploding)
    _write_fixture(tmp_path, "raises")
    loaded = eval_harness.load_fixtures(tmp_path)[0]

    with pytest.raises(RuntimeError, match="provider is down"):
        eval_harness.run_fixture(
            loaded,
            candidate_model="openai/gpt-4.1-mini",
            verifier_model="openai/gpt-4.1-mini",
        )
    assert review_engine._call_structured is exploding


def test_run_suite_refuses_a_cross_family_pair_without_a_second_rate_card(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "cross-family")
    fixtures = eval_harness.load_fixtures(tmp_path)

    with pytest.raises(ValueError, match="verifier_pricing"):
        eval_harness.run_suite(
            fixtures,
            name="cross-family",
            candidate_model="openai/gpt-4.1-mini",
            verifier_model="anthropic/claude-haiku-4-5",
            pricing=ModelPricing(input_usd_per_million_tokens=1),
        )


def test_run_suite_emits_a_scoreable_suite(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "alpha")
    _write_fixture(tmp_path, "beta")
    fixtures = eval_harness.load_fixtures(tmp_path)

    suite = eval_harness.run_suite(
        fixtures,
        name="plumbing",
        candidate_model="openai/gpt-4.1-mini",
        verifier_model="openai/gpt-4.1-mini",
        pricing=ModelPricing(
            input_usd_per_million_tokens=0.4,
            output_usd_per_million_tokens=1.6,
        ),
    )

    assert [case.case_id for case in suite.cases] == ["alpha", "beta"]
    score = score_evaluation(suite)
    assert score.recall == 1.0
    assert score.precision == 1.0
    assert score.estimated_cost_usd > 0


def test_every_committed_label_is_reachable_through_the_whole_engine(monkeypatch):
    """A perfect model scores 100% on the committed fixtures.

    `load_fixtures` already refuses a label that no changed line can carry, but
    that check reads the diff directly. This one pushes each label through
    `generate_review` itself -- candidate deduplication, `is_commentable`, the
    confidence threshold, the severity filter and the finding cap -- so a
    fixture that is unmatchable for any of those reasons fails here rather than
    showing up later as an unexplained recall ceiling in a captured baseline.

    It says nothing about whether a real model finds these bugs. That is what
    capturing a baseline measures.
    """

    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_PASSES", "security")
    fixtures = eval_harness.load_fixtures(FIXTURE_ROOT)

    for loaded in fixtures:
        parsed = parse_unified_diff(loaded.diff_text)
        candidates = []
        for expected in loaded.fixture.expected:
            placement = next(
                (
                    (expected.side, line)
                    for line in range(
                        expected.line - expected.line_tolerance,
                        expected.line + expected.line_tolerance + 1,
                    )
                    if line > 0
                    and parsed.is_commentable(expected.file_path, expected.side, line)
                ),
                None,
            )
            assert placement is not None, (
                f"{loaded.fixture.case_id}: no commentable line within "
                f"{expected.line_tolerance} of {expected.file_path}:{expected.line}"
            )
            side, line = placement
            candidates.append(
                CandidateFinding(
                    title=expected.title,
                    body="A concrete defect introduced by this change.",
                    severity=expected.severity or Severity.HIGH,
                    category=expected.category,
                    confidence=0.95,
                    file_path=expected.file_path,
                    line=line,
                    side=side,
                    evidence="The changed line introduces the defect.",
                )
            )
        _stub_call(
            monkeypatch,
            findings=candidates,
            keep={f"candidate-{index}" for index in range(len(candidates))},
        )
        case = eval_harness.run_fixture(
            loaded,
            candidate_model="openai/gpt-4.1-mini",
            verifier_model="openai/gpt-4.1-mini",
        )
        score = score_evaluation(
            EvaluationSuite(
                schema_version="diffuse-evaluation-v3",
                name=loaded.fixture.case_id,
                model="openai/gpt-4.1-mini",
                verifier_model="openai/gpt-4.1-mini",
                cases=[case],
            )
        )
        assert score.recall == 1.0, loaded.fixture.case_id
        assert score.precision == 1.0, loaded.fixture.case_id


# ---------------------------------------------------------------------------
# the regression gate
# ---------------------------------------------------------------------------


def _configuration(**overrides) -> RunConfiguration:
    return RunConfiguration(
        **{
            "prompt_version": "native-review-v6-review-diagrams",
            "min_review_confidence": 0.75,
            "review_passes": ["security"],
            "requested_review_depth": None,
            "depth_renderings": [],
            **overrides,
        }
    )


def _suite(
    *,
    observed_per_case: dict[str, list[ObservedFinding]],
    run_configuration: RunConfiguration | None = None,
    digest_per_case: dict[str, str] | None = None,
) -> EvaluationSuite:
    digests = digest_per_case or {}
    return EvaluationSuite(
        schema_version="diffuse-evaluation-v3",
        name="gate",
        model="openai/gpt-4.1-mini",
        run_configuration=run_configuration or _configuration(),
        cases=[
            EvaluationCase(
                case_id=case_id,
                expected=[
                    {
                        "finding_id": f"{case_id}-1",
                        "title": "Authorization bypass",
                        "file_path": "app.py",
                        "line": 1,
                        "side": "RIGHT",
                        "category": "security",
                    }
                ],
                observed=observed,
                latency_ms=10,
                fixture_digest=digests.get(case_id, f"digest-of-{case_id}"),
            )
            for case_id, observed in sorted(observed_per_case.items())
        ],
    )


def _finding(
    line: int = 1,
    category: str = "security",
    *,
    title: str = "Authorization bypass",
    side: str = "RIGHT",
) -> ObservedFinding:
    return ObservedFinding(
        title=title,
        file_path="app.py",
        line=line,
        side=side,
        category=Category(category),
        severity=Severity.HIGH,
    )


def test_an_identical_run_is_not_a_regression():
    suite = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(suite))
    assert eval_harness.compare_to_baseline(score_evaluation(suite), baseline) == []


def test_a_missed_finding_is_a_regression():
    good = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(good))
    worse = _suite(observed_per_case={"alpha": [_finding()], "beta": []})

    regressions = eval_harness.compare_to_baseline(score_evaluation(worse), baseline)

    assert any("case 'beta' missed 1 labeled findings" in line for line in regressions)
    assert any(line.startswith("recall fell to") for line in regressions)


def test_a_new_false_positive_is_a_regression():
    good = _suite(observed_per_case={"alpha": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(good))
    worse = _suite(observed_per_case={"alpha": [_finding(), _finding(line=40)]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(worse), baseline)

    assert any("reported 1 unlabeled findings" in line for line in regressions)


def test_tolerance_absorbs_a_small_aggregate_drop_but_not_a_case_regression():
    good = _suite(
        observed_per_case={
            "alpha": [_finding()],
            "beta": [_finding()],
            "gamma": [_finding()],
            "delta": [_finding()],
        }
    )
    baseline = eval_harness.baseline_from_score(score_evaluation(good))
    worse = _suite(
        observed_per_case={
            "alpha": [_finding()],
            "beta": [_finding()],
            "gamma": [_finding()],
            "delta": [],
        }
    )

    regressions = eval_harness.compare_to_baseline(
        score_evaluation(worse), baseline, tolerance=0.5
    )

    assert not any(line.startswith("recall fell to") for line in regressions)
    assert any("case 'delta' missed" in line for line in regressions)


def test_editing_a_fixtures_labels_after_capture_is_reported():
    """Dropping a stubbornly-missed label would otherwise 'improve' recall."""

    baseline = eval_harness.baseline_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    relabeled = _suite(observed_per_case={"alpha": [_finding()]})
    relabeled.cases[0].expected = []
    relabeled.cases[0].observed = []

    regressions = eval_harness.compare_to_baseline(score_evaluation(relabeled), baseline)

    assert any("now carries 0 labels" in line for line in regressions)


def test_a_fixture_with_no_baseline_entry_is_reported():
    baseline = eval_harness.baseline_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    wider = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(wider), baseline)

    assert any("has no baseline entry" in line for line in regressions)


def test_recategorising_a_found_defect_is_not_a_regression():
    """The gate measures whether the bug was found, not what it was called.

    Before DEV-292 this ran as a lost true positive *and* a gained false
    positive, so a run that found exactly the same defects would have failed the
    gate twice over for a taxonomy disagreement. Baselines are captured next, so
    the wrong answer here would have been frozen into them.
    """

    good = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(good))
    recategorised = _suite(
        observed_per_case={
            "alpha": [_finding()],
            "beta": [_finding(category="correctness")],
        }
    )

    score = score_evaluation(recategorised)

    assert eval_harness.compare_to_baseline(score, baseline) == []
    assert score.category_mismatches == 1
    assert [item.count for item in score.category_confusion] == [1]


def test_a_finding_that_drifts_out_of_the_span_is_still_a_regression():
    """Widening what counts as the same place must not hide a real miss."""

    good = _suite(observed_per_case={"alpha": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(good))
    # The label sits at line 1 with the default tolerance of 3.
    drifted = _suite(observed_per_case={"alpha": [_finding(line=5)]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(drifted), baseline)

    assert any("missed 1 labeled findings" in line for line in regressions)
    assert any("reported 1 unlabeled findings" in line for line in regressions)


def test_a_baseline_case_that_did_not_run_is_reported():
    baseline = eval_harness.baseline_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()], "beta": []}))
    )
    narrower = _suite(observed_per_case={"alpha": [_finding()]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(narrower), baseline)

    assert any("is in the baseline but was not run" in line for line in regressions)


def test_comparing_against_a_baseline_from_another_model_is_refused():
    suite = _suite(observed_per_case={"alpha": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(suite))
    other = baseline.model_copy(update={"model": "anthropic/claude-sonnet-5"})

    regressions = eval_harness.compare_to_baseline(score_evaluation(suite), other)

    assert any("is not meaningful" in line for line in regressions)


def test_a_baseline_captured_at_a_different_confidence_floor_is_refused():
    """The trigger is a variable left in a shell, not an exotic misuse.

    Capture with MIN_REVIEW_CONFIDENCE=0.99 and the baseline records near-zero
    recall and near-zero false positives, which every later run at the default
    0.75 clears trivially and forever -- with the precision guard pinned to a
    floor nobody chose.
    """

    strict = _suite(
        observed_per_case={"alpha": [_finding()]},
        run_configuration=_configuration(min_review_confidence=0.99),
    )
    baseline = eval_harness.baseline_from_score(score_evaluation(strict))
    default = _suite(observed_per_case={"alpha": [_finding()]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(default), baseline)

    assert any("MIN_REVIEW_CONFIDENCE" in line for line in regressions)
    assert any("not meaningful" in line for line in regressions)


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    (
        ("prompt_version", "native-review-v7-something-else", "PROMPT_VERSION"),
        ("review_passes", ["security", "correctness"], "REVIEW_PASSES"),
        ("requested_review_depth", "thorough", "review depth"),
    ),
)
def test_a_baseline_captured_under_other_configuration_is_refused(field, value, needle):
    captured = _suite(
        observed_per_case={"alpha": [_finding()]},
        run_configuration=_configuration(**{field: value}),
    )
    baseline = eval_harness.baseline_from_score(score_evaluation(captured))
    now = _suite(observed_per_case={"alpha": [_finding()]})

    regressions = eval_harness.compare_to_baseline(score_evaluation(now), baseline)

    assert any(needle in line for line in regressions)


def test_a_baseline_captured_at_a_depth_the_model_never_received_is_refused():
    """Asking for a depth and being sent one are different things.

    A route with no reasoning control is sent nothing whatever the request says,
    so two runs that agree on `REVIEW_DEPTH` can still differ on what reached the
    model.
    """

    honored = _configuration(
        requested_review_depth="thorough",
        depth_renderings=[
            {
                "stage": "candidate and verifier",
                "model": "anthropic/claude-sonnet-5",
                "mechanism": "effort-scale",
                "effort": "xhigh",
            }
        ],
    )
    dropped = _configuration(
        requested_review_depth="thorough",
        depth_renderings=[
            {
                "stage": "candidate and verifier",
                "model": "anthropic/claude-sonnet-5",
                "mechanism": "none",
                "effort": None,
            }
        ],
    )
    baseline = eval_harness.baseline_from_score(
        score_evaluation(
            _suite(observed_per_case={"alpha": [_finding()]}, run_configuration=honored)
        )
    )

    regressions = eval_harness.compare_to_baseline(
        score_evaluation(
            _suite(observed_per_case={"alpha": [_finding()]}, run_configuration=dropped)
        ),
        baseline,
    )

    assert any("candidate and verifier" in line for line in regressions)
    assert any("no reasoning parameter" in line for line in regressions)


def test_editing_a_fixtures_diff_after_capture_is_reported():
    """Making the bug more obvious raises recall without the engine changing.

    The label count cannot see it: the labels are untouched, only the code the
    reviewer is asked to read got easier.
    """

    baseline = eval_harness.baseline_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    rewritten = _suite(
        observed_per_case={"alpha": [_finding()]},
        digest_per_case={"alpha": "a-different-fixture-entirely"},
    )

    regressions = eval_harness.compare_to_baseline(score_evaluation(rewritten), baseline)

    assert any("different fixture content" in line for line in regressions)


def test_a_run_that_recorded_no_configuration_cannot_defend_a_baseline():
    baseline = eval_harness.baseline_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    anonymous = _suite(observed_per_case={"alpha": [_finding()]})
    anonymous.run_configuration = None

    regressions = eval_harness.compare_to_baseline(score_evaluation(anonymous), baseline)

    assert any("recorded no configuration" in line for line in regressions)


def test_a_baseline_cannot_be_captured_from_a_suite_with_no_configuration():
    suite = _suite(observed_per_case={"alpha": [_finding()]})
    suite.run_configuration = None

    with pytest.raises(ValueError, match="records no run configuration"):
        eval_harness.baseline_from_score(score_evaluation(suite))


def test_the_baseline_records_category_mismatches_and_reports_the_delta():
    """Taxonomy drift is recorded and named, and never fails the gate.

    A reviewer that starts filing every injection as `maintainability` is saying
    something worth reading, but it is a statement about taxonomy rather than
    about whether the bug was found -- and the table it comes from is
    non-minimal by construction.
    """

    agreeing = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(agreeing))
    assert baseline.category_mismatches == 0

    drifted = score_evaluation(
        _suite(
            observed_per_case={
                "alpha": [_finding(category="maintainability")],
                "beta": [_finding(category="maintainability")],
            }
        )
    )

    assert eval_harness.compare_to_baseline(drifted, baseline) == []
    delta = eval_harness.category_mismatch_delta(drifted, baseline)
    assert delta is not None
    assert "up from" in delta
    assert "security->maintainability" in delta


def test_tolerance_outside_zero_to_one_is_refused():
    suite = _suite(observed_per_case={"alpha": [_finding()]})
    baseline = eval_harness.baseline_from_score(score_evaluation(suite))
    with pytest.raises(ValueError, match="tolerance must be between 0 and 1"):
        eval_harness.compare_to_baseline(score_evaluation(suite), baseline, tolerance=2.0)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def test_check_exits_with_capture_instructions_when_no_baseline_exists(tmp_path, capsys):
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [_finding()]}).model_dump_json()
    )

    exit_code = eval_harness.main(
        [
            "check",
            "--suite",
            str(suite_path),
            "--baseline",
            str(tmp_path / "absent.json"),
        ]
    )

    assert exit_code == 2
    assert "evals/CAPTURE.md" in capsys.readouterr().err


def test_capture_then_check_round_trips(tmp_path):
    suite_path = tmp_path / "suite.json"
    baseline_path = tmp_path / "baselines" / "review-baseline.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
        .model_dump_json()
    )

    assert (
        eval_harness.main(
            ["capture", "--suite", str(suite_path), "--baseline", str(baseline_path)]
        )
        == 0
    )
    assert eval_harness.main(
        ["check", "--suite", str(suite_path), "--baseline", str(baseline_path)]
    ) == 0


def test_check_exits_one_on_a_seeded_regression(tmp_path, capsys):
    baseline_path = tmp_path / "baseline.json"
    good = tmp_path / "good.json"
    worse = tmp_path / "worse.json"
    good.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
        .model_dump_json()
    )
    worse.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": []}).model_dump_json()
    )
    eval_harness.main(["capture", "--suite", str(good), "--baseline", str(baseline_path)])

    with pytest.raises(SystemExit) as raised:
        eval_harness.main(
            ["check", "--suite", str(worse), "--baseline", str(baseline_path)]
        )

    assert raised.value.code == 1
    assert "REGRESSION" in capsys.readouterr().err


def test_capture_refuses_a_baseline_that_found_nothing(tmp_path, capsys):
    """A baseline with no true positives passes against every later run.

    Including one where the review engine returns nothing at all, because
    precision is 1.0 when there is nothing to be precise about. `scripts/eval.sh`
    and `evals/CAPTURE.md` both warn about this in prose; the plan asks for a
    machine check.
    """

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [], "beta": []}).model_dump_json()
    )

    exit_code = eval_harness.main(
        ["capture", "--suite", str(suite_path), "--baseline", str(tmp_path / "g.json")]
    )

    assert exit_code == 2
    assert "--allow-zero-recall" in capsys.readouterr().err
    assert not (tmp_path / "g.json").exists()


def test_capture_records_a_zero_recall_baseline_when_asked_explicitly(tmp_path):
    suite_path = tmp_path / "suite.json"
    baseline_path = tmp_path / "g.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [], "beta": []}).model_dump_json()
    )

    assert (
        eval_harness.main(
            [
                "capture",
                "--suite",
                str(suite_path),
                "--baseline",
                str(baseline_path),
                "--allow-zero-recall",
            ]
        )
        == 0
    )
    assert eval_harness.load_baseline(baseline_path).recall == 0.0


def test_run_records_the_configuration_that_moves_the_score(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_PASSES", "security")
    monkeypatch.setenv("MIN_REVIEW_CONFIDENCE", "0.6")
    monkeypatch.delenv("REVIEW_DEPTH", raising=False)
    monkeypatch.delenv("REVIEW_EFFORT", raising=False)
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "configured")
    output = tmp_path / "suite.json"

    assert (
        eval_harness.main(["run", "--fixtures", str(tmp_path), "--output", str(output)])
        == 0
    )

    configuration = eval_harness.load_suite(output).run_configuration
    assert configuration is not None
    assert configuration.min_review_confidence == 0.6
    assert configuration.review_passes == ["security"]
    assert configuration.prompt_version == review_engine.PROMPT_VERSION
    assert configuration.requested_review_depth is None


def test_run_records_what_the_model_was_actually_sent_for_a_depth(
    tmp_path, monkeypatch
):
    """`CAPTURE.md` recommends a cheap model, and the cheap one honours nothing.

    A baseline that recorded only the requested depth would let a run at
    `REVIEW_DEPTH=thorough` on a route with no reasoning control defend a run at
    the same depth on a route that has one.
    """

    monkeypatch.setenv("REVIEW_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_PASSES", "security")
    monkeypatch.setenv("REVIEW_DEPTH", "thorough")
    monkeypatch.delenv("REVIEW_EFFORT", raising=False)
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "deep")
    output = tmp_path / "suite.json"

    assert (
        eval_harness.main(["run", "--fixtures", str(tmp_path), "--output", str(output)])
        == 0
    )

    configuration = eval_harness.load_suite(output).run_configuration
    assert configuration is not None
    assert configuration.requested_review_depth == "thorough"
    assert configuration.depth_renderings
    assert all(
        rendering.effort is not None for rendering in configuration.depth_renderings
    )


def test_run_refuses_a_depth_the_candidate_model_cannot_express(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_DEPTH", "thorough")
    monkeypatch.delenv("REVIEW_EFFORT", raising=False)

    def never(*_args, **_kwargs):
        raise AssertionError("a model was called at a depth it cannot express")

    monkeypatch.setattr(review_engine, "_call_structured", never)
    _write_fixture(tmp_path, "undeliverable-depth")

    exit_code = eval_harness.main(
        ["run", "--fixtures", str(tmp_path), "--output", str(tmp_path / "suite.json")]
    )

    assert exit_code == 2
    assert "cannot be honored" in capsys.readouterr().err
    assert not (tmp_path / "suite.json").exists()


def test_run_resumes_from_the_cases_a_previous_attempt_paid_for(
    tmp_path, monkeypatch
):
    """A provider error on fixture 7 of 8 used to discard 35 completed calls."""

    fixtures = tmp_path / "fixtures"
    _write_fixture(fixtures, "alpha")
    _write_fixture(fixtures, "beta")
    output = tmp_path / "suite.json"
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_PASSES", "security")

    calls: list[str] = []
    real_run_fixture = eval_harness.run_fixture

    def counting(loaded, **kwargs):
        calls.append(loaded.fixture.case_id)
        if loaded.fixture.case_id == "beta" and len(calls) == 2:
            raise RuntimeError("provider is down")
        return real_run_fixture(loaded, **kwargs)

    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    monkeypatch.setattr(eval_harness, "run_fixture", counting)

    with pytest.raises(RuntimeError, match="provider is down"):
        eval_harness.main(
            ["run", "--fixtures", str(fixtures), "--output", str(output)]
        )
    assert calls == ["alpha", "beta"]
    # The partial file is the point: alpha's model calls are already paid for.
    assert [case["case_id"] for case in json.loads(output.read_text())["cases"]] == [
        "alpha"
    ]

    calls.clear()
    assert (
        eval_harness.main(
            ["run", "--fixtures", str(fixtures), "--output", str(output), "--resume"]
        )
        == 0
    )

    assert calls == ["beta"]
    suite = eval_harness.load_suite(output)
    assert [case.case_id for case in suite.cases] == ["alpha", "beta"]


def test_resuming_re_runs_a_case_whose_fixture_changed(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    directory = _write_fixture(fixtures, "alpha")
    output = tmp_path / "suite.json"
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_PASSES", "security")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})

    assert (
        eval_harness.main(["run", "--fixtures", str(fixtures), "--output", str(output)])
        == 0
    )
    (directory / "diff.patch").write_text(DIFF.replace("user_value", "other_value"))

    calls: list[str] = []
    real_run_fixture = eval_harness.run_fixture
    monkeypatch.setattr(
        eval_harness,
        "run_fixture",
        lambda loaded, **kwargs: (
            calls.append(loaded.fixture.case_id),
            real_run_fixture(loaded, **kwargs),
        )[1],
    )

    assert (
        eval_harness.main(
            ["run", "--fixtures", str(fixtures), "--output", str(output), "--resume"]
        )
        == 0
    )
    assert calls == ["alpha"]


def test_run_refuses_a_cross_family_pair_before_calling_any_model(
    tmp_path, monkeypatch, capsys
):
    """The pricing refusal has to come first, or it arrives after the bill."""

    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", "anthropic/claude-haiku-4-5")

    def never(*_args, **_kwargs):
        raise AssertionError("a model was called despite unresolved pricing")

    monkeypatch.setattr(review_engine, "_call_structured", never)
    _write_fixture(tmp_path, "priced")

    exit_code = eval_harness.main(
        [
            "run",
            "--fixtures",
            str(tmp_path),
            "--output",
            str(tmp_path / "suite.json"),
        ]
    )

    assert exit_code == 2
    assert "--verifier-input-usd-per-million" in capsys.readouterr().err
    assert not (tmp_path / "suite.json").exists()


def test_run_refuses_when_review_model_is_unset(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REVIEW_MODEL", raising=False)
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)

    def never(*_args, **_kwargs):
        raise AssertionError("a model was called with REVIEW_MODEL unset")

    monkeypatch.setattr(review_engine, "_call_structured", never)
    _write_fixture(tmp_path, "unset-model")

    exit_code = eval_harness.main(
        [
            "run",
            "--fixtures",
            str(tmp_path),
            "--output",
            str(tmp_path / "suite.json"),
        ]
    )

    assert exit_code == 2
    assert "REVIEW_MODEL is not set" in capsys.readouterr().err


def test_run_writes_a_suite_the_scorer_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.setenv("REVIEW_PASSES", "security")
    _stub_call(monkeypatch, findings=[_candidate()], keep={"candidate-0"})
    _write_fixture(tmp_path, "written")
    output = tmp_path / "out" / "suite.json"

    assert (
        eval_harness.main(
            ["run", "--fixtures", str(tmp_path), "--output", str(output)]
        )
        == 0
    )

    suite = eval_harness.load_suite(output)
    assert suite.model == "openai/gpt-4.1-mini"
    assert score_evaluation(suite).recall == 1.0
