import argparse
import json

import pytest
from pydantic import ValidationError

from service import evaluation_cli
from service.evaluation import EvaluationSuite, score_evaluation


def test_evaluation_scores_quality_latency_cost_and_addressed_findings() -> None:
    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v1",
            "name": "unit baseline",
            "model": "openai/test",
            "pricing": {
                "input_usd_per_million_tokens": 2,
                "output_usd_per_million_tokens": 8,
            },
            "cases": [
                {
                    "case_id": "one",
                    "expected": [
                        {
                            "finding_id": "bug-1",
                            "file_path": "service/api.py",
                            "line": 20,
                            "category": "correctness",
                            "severity": "high",
                        },
                        {
                            "finding_id": "bug-2",
                            "file_path": "service/api.py",
                            "line": 80,
                            "category": "security",
                        },
                    ],
                    "observed": [
                        {
                            "file_path": "service/api.py",
                            "line": 22,
                            "category": "correctness",
                            "severity": "high",
                        },
                        {
                            "file_path": "README.md",
                            "line": 4,
                            "category": "maintainability",
                            "severity": "low",
                        },
                    ],
                    "addressed_finding_ids": ["bug-1"],
                    "latency_ms": 100,
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
                {
                    "case_id": "two",
                    "expected": [],
                    "observed": [],
                    "latency_ms": 200,
                },
            ],
        }
    )

    score = score_evaluation(suite)

    assert score.true_positives == 1
    assert score.false_positives == 1
    assert score.false_negatives == 1
    assert score.addressed_findings == 1
    assert score.precision == 0.5
    assert score.recall == 0.5
    assert score.f1 == 0.5
    assert score.median_latency_ms == 150
    assert score.estimated_cost_usd == 0.006


def test_observed_finding_cannot_match_multiple_labels() -> None:
    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v1",
            "name": "duplicate guard",
            "model": "test",
            "cases": [
                {
                    "case_id": "case",
                    "expected": [
                        {
                            "finding_id": "one",
                            "file_path": "a.py",
                            "line": 10,
                            "category": "correctness",
                        },
                        {
                            "finding_id": "two",
                            "file_path": "a.py",
                            "line": 12,
                            "category": "correctness",
                        },
                    ],
                    "observed": [
                        {
                            "file_path": "a.py",
                            "line": 11,
                            "category": "correctness",
                            "severity": "medium",
                        }
                    ],
                }
            ],
        }
    )

    score = score_evaluation(suite)

    assert score.true_positives == 1
    assert score.false_negatives == 1


def _overlapping_suite(
    expected: list[dict],
    observed: list[dict],
    *,
    addressed: list[str] | None = None,
) -> EvaluationSuite:
    return EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v1",
            "name": "overlapping labels",
            "model": "test",
            "cases": [
                {
                    "case_id": "case",
                    "expected": expected,
                    "observed": observed,
                    "addressed_finding_ids": addressed or [],
                }
            ],
        }
    )


_LABEL_13 = {
    "finding_id": "near",
    "file_path": "auth.py",
    "line": 13,
    "category": "correctness",
}
_LABEL_10 = {
    "finding_id": "far",
    "file_path": "auth.py",
    "line": 10,
    "category": "correctness",
}
_OBSERVED_13 = {
    "file_path": "auth.py",
    "line": 13,
    "category": "correctness",
    "severity": "medium",
}
_OBSERVED_16 = {
    "file_path": "auth.py",
    "line": 16,
    "category": "correctness",
    "severity": "medium",
}


def test_overlapping_tolerances_use_maximum_cardinality_matching() -> None:
    """Both labels are reported, so both must be credited.

    Nearest-first assignment gives the line-13 observation to the line-13 label,
    starving the line-10 label whose only remaining candidate is 6 lines away,
    and charges that miss as both a false negative and a false positive. This
    fires at the default line tolerance of 3.
    """

    for expected in ([_LABEL_13, _LABEL_10], [_LABEL_10, _LABEL_13]):
        score = score_evaluation(
            _overlapping_suite(expected, [_OBSERVED_13, _OBSERVED_16])
        )

        assert score.true_positives == 2
        assert score.false_positives == 0
        assert score.false_negatives == 0
        assert score.precision == 1.0
        assert score.recall == 1.0


def test_matching_is_stable_under_observed_reordering() -> None:
    """The scored assignment must not depend on input ordering."""

    scores = [
        score_evaluation(_overlapping_suite([_LABEL_13, _LABEL_10], observed))
        for observed in ([_OBSERVED_13, _OBSERVED_16], [_OBSERVED_16, _OBSERVED_13])
    ]

    assert scores[0].true_positives == scores[1].true_positives == 2
    assert scores[0].addressed_findings == scores[1].addressed_findings


def test_a_stricter_label_is_not_starved_by_a_looser_one() -> None:
    """A tolerance-0 label must still match its exact-line observation."""

    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "loose",
                    "file_path": "f.py",
                    "line": 10,
                    "category": "correctness",
                    "line_tolerance": 5,
                },
                {
                    "finding_id": "strict",
                    "file_path": "f.py",
                    "line": 10,
                    "category": "correctness",
                    "line_tolerance": 0,
                },
            ],
            [
                {
                    "file_path": "f.py",
                    "line": 10,
                    "category": "correctness",
                    "severity": "medium",
                },
                {
                    "file_path": "f.py",
                    "line": 14,
                    "category": "correctness",
                    "severity": "medium",
                },
            ],
        )
    )

    assert score.true_positives == 2
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_addressed_findings_do_not_depend_on_label_order() -> None:
    """Which of two competing labels gets the one observation must be decided.

    Regression: maximum cardinality alone left the choice to `expected` list
    order, so a suite reporting one addressed finding turned into a suite
    reporting none purely by reordering identical labels.
    """

    scores = [
        score_evaluation(
            _overlapping_suite(expected, [_OBSERVED_13], addressed=["far"])
        )
        for expected in ([_LABEL_13, _LABEL_10], [_LABEL_10, _LABEL_13])
    ]

    assert scores[0].true_positives == scores[1].true_positives == 1
    assert scores[0].addressed_findings == scores[1].addressed_findings == 1


def test_prioritising_addressed_labels_never_costs_a_match() -> None:
    """The addressed tie-break must not shrink the matching it breaks ties in."""

    score = score_evaluation(
        _overlapping_suite(
            [_LABEL_13, _LABEL_10],
            [_OBSERVED_13, _OBSERVED_16],
            addressed=["far"],
        )
    )

    assert score.true_positives == 2
    assert score.false_negatives == 0
    assert score.addressed_findings == 1


def test_candidate_and_verifier_tokens_are_priced_at_their_own_rates() -> None:
    """A cross-family pair bills two rates, so one rate cannot cover both."""

    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v1",
            "name": "cross-family cost",
            "model": "openai/candidate",
            "pricing": {
                "input_usd_per_million_tokens": 2,
                "output_usd_per_million_tokens": 8,
            },
            "verifier_model": "anthropic/verifier",
            "verifier_pricing": {
                "input_usd_per_million_tokens": 30,
                "output_usd_per_million_tokens": 150,
            },
            "cases": [
                {
                    "case_id": "one",
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 1_000_000,
                    "verifier_prompt_tokens": 1_000_000,
                    "verifier_completion_tokens": 1_000_000,
                }
            ],
        }
    )

    score = score_evaluation(suite)

    assert score.verifier_model == "anthropic/verifier"
    assert score.candidate_prompt_tokens == 1_000_000
    assert score.verifier_completion_tokens == 1_000_000
    assert score.prompt_tokens == 2_000_000
    assert score.completion_tokens == 2_000_000
    assert score.estimated_cost_usd == 2 + 8 + 30 + 150


def test_shared_verifier_model_reuses_the_candidate_rates() -> None:
    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v1",
            "name": "single model",
            "model": "openai/candidate",
            "pricing": {
                "input_usd_per_million_tokens": 2,
                "output_usd_per_million_tokens": 8,
            },
            "verifier_model": "openai/candidate",
            "cases": [
                {
                    "case_id": "one",
                    "prompt_tokens": 1_000_000,
                    "verifier_completion_tokens": 1_000_000,
                }
            ],
        }
    )

    assert score_evaluation(suite).estimated_cost_usd == 10


@pytest.mark.parametrize(
    "override",
    (
        # Verifier tokens with nothing to price them against.
        {"cases": [{"case_id": "one", "verifier_prompt_tokens": 10}]},
        # A distinct verifier model whose rates were never stated.
        {"verifier_model": "anthropic/verifier"},
        # Rates for a verifier the suite never names.
        {
            "verifier_pricing": {
                "input_usd_per_million_tokens": 30,
                "output_usd_per_million_tokens": 150,
            }
        },
    ),
)
def test_unpriceable_verifier_labels_are_rejected(override: dict) -> None:
    payload = {
        "schema_version": "diffuse-evaluation-v1",
        "name": "incomplete pricing",
        "model": "openai/candidate",
        "cases": [{"case_id": "one"}],
        **override,
    }

    with pytest.raises(ValidationError):
        EvaluationSuite.model_validate(payload)


def test_path_and_category_still_constrain_matches() -> None:
    score = score_evaluation(
        _overlapping_suite(
            [_LABEL_13],
            [
                {
                    "file_path": "other.py",
                    "line": 13,
                    "category": "correctness",
                    "severity": "medium",
                },
                {
                    "file_path": "auth.py",
                    "line": 13,
                    "category": "security",
                    "severity": "medium",
                },
            ],
        )
    )

    assert score.true_positives == 0
    assert score.false_negatives == 1
    assert score.false_positives == 2


def _evaluate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diffuse evaluate")
    evaluation_cli.configure_parser(parser)
    return parser


@pytest.mark.parametrize("value", ("nan", "-nan", "inf", "-inf", "1.5", "-0.1", "high"))
def test_a_threshold_that_cannot_gate_is_rejected(value: str) -> None:
    """`--min-f1 nan` compared false against every score and passed silently.

    A release gate that cannot fail is worse than no gate, so the malformed
    value must be refused at parse time rather than applied.
    """

    with pytest.raises(SystemExit):
        _evaluate_parser().parse_args(["suite.json", "--min-f1", value])


def test_valid_thresholds_still_gate_a_measured_score(tmp_path, capsys) -> None:
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "schema_version": "diffuse-evaluation-v1",
                "name": "gate",
                "model": "openai/test",
                "cases": [
                    {
                        "case_id": "miss",
                        "expected": [
                            {
                                "finding_id": "bug-1",
                                "file_path": "a.py",
                                "line": 5,
                                "category": "correctness",
                            }
                        ],
                    }
                ],
            }
        )
    )
    parser = _evaluate_parser()

    args = parser.parse_args([str(suite), "--min-f1", "0.5"])
    with pytest.raises(SystemExit) as failure:
        args.handler(args)

    assert failure.value.code == 1
    assert json.loads(capsys.readouterr().out)["recall"] == 0.0

    passing = parser.parse_args([str(suite), "--min-f1", "0"])
    passing.handler(passing)
