"""Plumbing tests for the golden-output harness.

These verify that the harness assembles the `observed` structure
`service/evaluation.py` consumes, that it refuses fixtures whose labels the
review engine could never satisfy, and that the golden comparison actually
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

from service import eval_harness, review_engine
from service.diff_parser import parse_unified_diff
from service.evaluation import (
    EvaluationCase,
    EvaluationSuite,
    ModelPricing,
    ObservedFinding,
    score_evaluation,
)
from service.review_models import (
    CandidateBatch,
    CandidateFinding,
    Category,
    SecurityClassification,
    Severity,
    VerificationBatch,
    VerificationDecision,
)

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
        "schema_version": "diffuse-eval-fixture-v1",
        "case_id": case_id_in_json or case_id,
        "description": "A synthetic fixture used only to exercise the harness.",
        "expected": expected
        if expected is not None
        else [
            {
                "finding_id": f"{case_id}-1",
                "file_path": "app.py",
                "line": 1,
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
    assert len(fixtures) >= 5, "the unit calls for 5-10 committed fixtures"
    assert [loaded.fixture.case_id for loaded in fixtures] == sorted(
        loaded.fixture.case_id for loaded in fixtures
    )
    for loaded in fixtures:
        assert loaded.diff_text.startswith("diff --git ")
        assert loaded.contexts, f"{loaded.fixture.case_id} has no retrieved context"


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

    That fixture labels `service/webhook.py:42` and ships no diff, so its
    recorded run scores 0% recall no matter how good the review engine is.
    """

    _write_fixture(
        tmp_path,
        "unreachable-label",
        expected=[
            {
                "finding_id": "unreachable-1",
                "file_path": "app.py",
                "line": 42,
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
                "file_path": "service/webhook.py",
                "line": 1,
                "category": "security",
                "line_tolerance": 3,
            }
        ],
    )
    with pytest.raises(eval_harness.FixtureError, match="which the diff does not change"):
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
            file_path="app.py",
            line=1,
            category=Category.SECURITY,
            severity=Severity.HIGH,
            fingerprint=case.observed[0].fingerprint,
        )
    ]
    assert case.observed[0].fingerprint is not None
    # The scorer accepts it without any transcription step.
    score = score_evaluation(
        EvaluationSuite(
            schema_version="diffuse-evaluation-v1",
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
    showing up later as an unexplained recall ceiling in a captured golden.

    It says nothing about whether a real model finds these bugs. That is what
    capturing a golden measures.
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
                    (side, line)
                    for side in ("RIGHT", "LEFT")
                    for line in range(
                        expected.line - expected.line_tolerance,
                        expected.line + expected.line_tolerance + 1,
                    )
                    if line > 0
                    and parsed.is_commentable(expected.file_path, side, line)
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
                    title=f"Defect at {expected.file_path}:{line}",
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
                schema_version="diffuse-evaluation-v1",
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


def _suite(*, observed_per_case: dict[str, list[ObservedFinding]]) -> EvaluationSuite:
    return EvaluationSuite(
        schema_version="diffuse-evaluation-v1",
        name="gate",
        model="openai/gpt-4.1-mini",
        cases=[
            EvaluationCase(
                case_id=case_id,
                expected=[
                    {
                        "finding_id": f"{case_id}-1",
                        "file_path": "app.py",
                        "line": 1,
                        "category": "security",
                    }
                ],
                observed=observed,
                latency_ms=10,
            )
            for case_id, observed in sorted(observed_per_case.items())
        ],
    )


def _finding(line: int = 1, category: str = "security") -> ObservedFinding:
    return ObservedFinding(
        file_path="app.py",
        line=line,
        category=Category(category),
        severity=Severity.HIGH,
    )


def test_an_identical_run_is_not_a_regression():
    suite = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(suite))
    assert eval_harness.compare_to_golden(score_evaluation(suite), golden) == []


def test_a_missed_finding_is_a_regression():
    good = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(good))
    worse = _suite(observed_per_case={"alpha": [_finding()], "beta": []})

    regressions = eval_harness.compare_to_golden(score_evaluation(worse), golden)

    assert any("case 'beta' missed 1 labeled findings" in line for line in regressions)
    assert any(line.startswith("recall fell to") for line in regressions)


def test_a_new_false_positive_is_a_regression():
    good = _suite(observed_per_case={"alpha": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(good))
    worse = _suite(observed_per_case={"alpha": [_finding(), _finding(line=40)]})

    regressions = eval_harness.compare_to_golden(score_evaluation(worse), golden)

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
    golden = eval_harness.golden_from_score(score_evaluation(good))
    worse = _suite(
        observed_per_case={
            "alpha": [_finding()],
            "beta": [_finding()],
            "gamma": [_finding()],
            "delta": [],
        }
    )

    regressions = eval_harness.compare_to_golden(
        score_evaluation(worse), golden, tolerance=0.5
    )

    assert not any(line.startswith("recall fell to") for line in regressions)
    assert any("case 'delta' missed" in line for line in regressions)


def test_editing_a_fixtures_labels_after_capture_is_reported():
    """Dropping a stubbornly-missed label would otherwise 'improve' recall."""

    golden = eval_harness.golden_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    relabeled = _suite(observed_per_case={"alpha": [_finding()]})
    relabeled.cases[0].expected = []
    relabeled.cases[0].observed = []

    regressions = eval_harness.compare_to_golden(score_evaluation(relabeled), golden)

    assert any("now carries 0 labels" in line for line in regressions)


def test_a_fixture_with_no_golden_entry_is_reported():
    golden = eval_harness.golden_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()]}))
    )
    wider = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})

    regressions = eval_harness.compare_to_golden(score_evaluation(wider), golden)

    assert any("has no golden entry" in line for line in regressions)


def test_recategorising_a_found_defect_is_not_a_regression():
    """The gate measures whether the bug was found, not what it was called.

    Before DEV-292 this ran as a lost true positive *and* a gained false
    positive, so a run that found exactly the same defects would have failed the
    gate twice over for a taxonomy disagreement. Goldens are captured next, so
    the wrong answer here would have been frozen into them.
    """

    good = _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(good))
    recategorised = _suite(
        observed_per_case={
            "alpha": [_finding()],
            "beta": [_finding(category="correctness")],
        }
    )

    score = score_evaluation(recategorised)

    assert eval_harness.compare_to_golden(score, golden) == []
    assert score.category_mismatches == 1
    assert [item.count for item in score.category_confusion] == [1]


def test_a_finding_that_drifts_out_of_the_span_is_still_a_regression():
    """Widening what counts as the same place must not hide a real miss."""

    good = _suite(observed_per_case={"alpha": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(good))
    # The label sits at line 1 with the default tolerance of 3.
    drifted = _suite(observed_per_case={"alpha": [_finding(line=5)]})

    regressions = eval_harness.compare_to_golden(score_evaluation(drifted), golden)

    assert any("missed 1 labeled findings" in line for line in regressions)
    assert any("reported 1 unlabeled findings" in line for line in regressions)


def test_a_golden_case_that_did_not_run_is_reported():
    golden = eval_harness.golden_from_score(
        score_evaluation(_suite(observed_per_case={"alpha": [_finding()], "beta": []}))
    )
    narrower = _suite(observed_per_case={"alpha": [_finding()]})

    regressions = eval_harness.compare_to_golden(score_evaluation(narrower), golden)

    assert any("is in the golden but was not run" in line for line in regressions)


def test_comparing_against_a_golden_from_another_model_is_refused():
    suite = _suite(observed_per_case={"alpha": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(suite))
    other = golden.model_copy(update={"model": "anthropic/claude-sonnet-5"})

    regressions = eval_harness.compare_to_golden(score_evaluation(suite), other)

    assert any("is not meaningful" in line for line in regressions)


def test_tolerance_outside_zero_to_one_is_refused():
    suite = _suite(observed_per_case={"alpha": [_finding()]})
    golden = eval_harness.golden_from_score(score_evaluation(suite))
    with pytest.raises(ValueError, match="tolerance must be between 0 and 1"):
        eval_harness.compare_to_golden(score_evaluation(suite), golden, tolerance=2.0)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def test_check_exits_with_capture_instructions_when_no_golden_exists(tmp_path, capsys):
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [_finding()]}).model_dump_json()
    )

    exit_code = eval_harness.main(
        [
            "check",
            "--suite",
            str(suite_path),
            "--golden",
            str(tmp_path / "absent.json"),
        ]
    )

    assert exit_code == 2
    assert "evals/CAPTURE.md" in capsys.readouterr().err


def test_capture_then_check_round_trips(tmp_path):
    suite_path = tmp_path / "suite.json"
    golden_path = tmp_path / "golden" / "review-baseline.json"
    suite_path.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
        .model_dump_json()
    )

    assert (
        eval_harness.main(
            ["capture", "--suite", str(suite_path), "--golden", str(golden_path)]
        )
        == 0
    )
    assert eval_harness.main(
        ["check", "--suite", str(suite_path), "--golden", str(golden_path)]
    ) == 0


def test_check_exits_one_on_a_seeded_regression(tmp_path, capsys):
    golden_path = tmp_path / "golden.json"
    good = tmp_path / "good.json"
    worse = tmp_path / "worse.json"
    good.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": [_finding()]})
        .model_dump_json()
    )
    worse.write_text(
        _suite(observed_per_case={"alpha": [_finding()], "beta": []}).model_dump_json()
    )
    eval_harness.main(["capture", "--suite", str(good), "--golden", str(golden_path)])

    with pytest.raises(SystemExit) as raised:
        eval_harness.main(
            ["check", "--suite", str(worse), "--golden", str(golden_path)]
        )

    assert raised.value.code == 1
    assert "REGRESSION" in capsys.readouterr().err


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
