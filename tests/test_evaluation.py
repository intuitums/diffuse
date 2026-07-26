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


def _overlapping_suite(expected: list[dict], observed: list[dict]) -> EvaluationSuite:
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
