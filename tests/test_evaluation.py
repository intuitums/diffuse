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
