import argparse
import itertools
import json

import pytest
from pydantic import ValidationError

from service import evaluation_cli
from service.evaluation import (
    MAX_LINE_TOLERANCE,
    EvaluationSuite,
    score_evaluation,
)
from service.review_models import Category


def test_evaluation_scores_quality_latency_cost_and_addressed_findings() -> None:
    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v2",
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
            "schema_version": "diffuse-evaluation-v2",
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
            "schema_version": "diffuse-evaluation-v2",
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
            "schema_version": "diffuse-evaluation-v2",
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
            "schema_version": "diffuse-evaluation-v2",
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
        "schema_version": "diffuse-evaluation-v2",
        "name": "incomplete pricing",
        "model": "openai/candidate",
        "cases": [{"case_id": "one"}],
        **override,
    }

    with pytest.raises(ValidationError):
        EvaluationSuite.model_validate(payload)


def test_a_different_file_never_matches_however_close_the_line() -> None:
    """File equality is absolute. The same line number elsewhere is elsewhere."""

    score = score_evaluation(
        _overlapping_suite(
            [_LABEL_13],
            [
                # Same line, same category, wrong file.
                {
                    "file_path": "other.py",
                    "line": 13,
                    "category": "correctness",
                    "severity": "medium",
                },
                # Same line, same category, wrong file, and a path that is a
                # prefix of the labeled one -- still a different file.
                {
                    "file_path": "auth.pyi",
                    "line": 13,
                    "category": "correctness",
                    "severity": "medium",
                },
            ],
        )
    )

    assert score.true_positives == 0
    assert score.false_negatives == 1
    assert score.false_positives == 2
    assert score.category_mismatches == 0


def test_an_unrelated_finding_in_the_labeled_file_does_not_match() -> None:
    """Right file, outside the label's span: a miss and a false positive."""

    score = score_evaluation(
        _overlapping_suite(
            [_LABEL_13],  # line 13, default tolerance 3 -> lines 10..16
            [
                {
                    "file_path": "auth.py",
                    "line": 17,
                    "category": "correctness",
                    "severity": "medium",
                },
                {
                    "file_path": "auth.py",
                    "line": 9,
                    "category": "correctness",
                    "severity": "medium",
                },
            ],
        )
    )

    assert score.true_positives == 0
    assert score.false_negatives == 1
    assert score.false_positives == 2


def test_the_right_finding_in_the_wrong_category_is_one_true_positive() -> None:
    """The bug this whole matcher change exists to fix.

    A real SQL injection filed under `correctness` used to score as a false
    negative *and* a false positive: strictly worse than not finding it at all.
    It is one true positive, and the disagreement is reported separately.
    """

    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "injection",
                    "file_path": "reports/query.py",
                    "line": 23,
                    "category": "security",
                }
            ],
            [
                {
                    "file_path": "reports/query.py",
                    "line": 23,
                    "category": "correctness",
                    "severity": "high",
                }
            ],
        )
    )

    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.category_mismatches == 1
    assert [
        (item.expected, item.observed, item.count) for item in score.category_confusion
    ] == [(Category.SECURITY, Category.CORRECTNESS, 1)]
    assert [
        (item.finding_id, item.expected, item.observed)
        for item in score.cases[0].category_mismatches
    ] == [("injection", Category.SECURITY, Category.CORRECTNESS)]


def test_a_miscategorised_detection_outscores_a_missed_one() -> None:
    """Being right in the wrong taxonomy must beat being wrong."""

    label = {
        "finding_id": "injection",
        "file_path": "reports/query.py",
        "line": 23,
        "category": "security",
    }
    miscategorised = score_evaluation(
        _overlapping_suite(
            [label],
            [
                {
                    "file_path": "reports/query.py",
                    "line": 23,
                    "category": "correctness",
                    "severity": "high",
                }
            ],
        )
    )
    missed = score_evaluation(_overlapping_suite([label], []))

    assert miscategorised.f1 > missed.f1
    assert miscategorised.recall > missed.recall
    assert missed.category_mismatches == 0


def test_a_category_mismatch_still_counts_as_an_addressed_finding() -> None:
    score = score_evaluation(
        _overlapping_suite(
            [_LABEL_13],
            [
                {
                    "file_path": "auth.py",
                    "line": 13,
                    "category": "security",
                    "severity": "medium",
                }
            ],
            addressed=["near"],
        )
    )

    assert score.true_positives == 1
    assert score.addressed_findings == 1
    assert score.category_mismatches == 1


def test_category_confusion_aggregates_across_cases_by_frequency() -> None:
    """A model that systematically miscategorises should be legible at a glance."""

    def case(case_id: str, observed_category: str) -> dict:
        return {
            "case_id": case_id,
            "expected": [
                {
                    "finding_id": f"{case_id}-1",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                }
            ],
            "observed": [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": observed_category,
                    "severity": "high",
                }
            ],
        }

    suite = EvaluationSuite.model_validate(
        {
            "schema_version": "diffuse-evaluation-v2",
            "name": "confusion",
            "model": "test",
            "cases": [
                case("one", "correctness"),
                case("two", "correctness"),
                case("three", "reliability"),
                case("four", "security"),
            ],
        }
    )

    score = score_evaluation(suite)

    assert score.true_positives == 4
    assert score.false_positives == 0
    assert score.recall == 1.0
    assert score.category_mismatches == 3
    assert [
        (item.expected, item.observed, item.count) for item in score.category_confusion
    ] == [
        (Category.SECURITY, Category.CORRECTNESS, 2),
        (Category.SECURITY, Category.RELIABILITY, 1),
    ]


# ---------------------------------------------------------------------------
# over-matching
#
# Loosening the match key inflates recall and suppresses false positives if it
# lets one observation stand in for two labels, or one label swallow two
# observations. Both directions make the reviewer look better than it is, which
# is precisely the direction a regression gate must not fail in, so each is
# pinned explicitly below.
# ---------------------------------------------------------------------------


def test_two_distinct_labels_on_adjacent_lines_stay_distinct() -> None:
    """Two real defects three lines apart are two findings, not one."""

    labels = [
        {
            "finding_id": "first",
            "file_path": "a.py",
            "line": 10,
            "category": "security",
        },
        {
            "finding_id": "second",
            "file_path": "a.py",
            "line": 12,
            "category": "correctness",
        },
    ]
    # Every observation lies inside both labels' spans, and neither category
    # gates any longer, so the graph is fully connected. Matching must still be
    # one-to-one.
    both = score_evaluation(
        _overlapping_suite(
            labels,
            [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "high",
                },
                {
                    "file_path": "a.py",
                    "line": 12,
                    "category": "correctness",
                    "severity": "high",
                },
            ],
        )
    )

    assert both.true_positives == 2
    assert both.false_positives == 0
    assert both.false_negatives == 0
    assert both.category_mismatches == 0

    # Finding only one of the two must stay a miss. If the single observation
    # could satisfy both labels, recall would read 1.0 for half the work.
    one = score_evaluation(
        _overlapping_suite(
            labels,
            [
                {
                    "file_path": "a.py",
                    "line": 11,
                    "category": "security",
                    "severity": "high",
                }
            ],
        )
    )

    assert one.true_positives == 1
    assert one.false_negatives == 1
    assert one.false_positives == 0
    assert one.recall == 0.5


def test_one_label_cannot_absorb_two_observations() -> None:
    """A second finding inside the span is a false positive, not free credit."""

    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "only",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                }
            ],
            [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "high",
                },
                {
                    "file_path": "a.py",
                    "line": 11,
                    "category": "correctness",
                    "severity": "low",
                },
                {
                    "file_path": "a.py",
                    "line": 12,
                    "category": "maintainability",
                    "severity": "low",
                },
            ],
        )
    )

    assert score.true_positives == 1
    assert score.false_positives == 2
    assert score.precision == pytest.approx(1 / 3)
    # The exact-category observation was preferred, so no mismatch is invented
    # for what would only have been an assignment artifact.
    assert score.category_mismatches == 0


@pytest.mark.parametrize("label_count", (2, 3, 8))
def test_a_fully_connected_case_never_scores_more_matches_than_observations(
    label_count: int,
) -> None:
    """True positives are bounded by observations at any tolerance.

    Every label sits within every other label's span and the categories are all
    different, so under the new match key the bipartite graph is complete. Even
    then, one fewer observation than labels must cost exactly one miss.
    """

    categories = [
        "correctness",
        "security",
        "performance",
        "reliability",
        "testing",
        "architecture",
        "maintainability",
        "api",
    ]
    labels = [
        {
            "finding_id": f"label-{index}",
            "file_path": "a.py",
            "line": 10 + index,
            "category": categories[index],
            "line_tolerance": 10,
        }
        for index in range(label_count)
    ]
    observed = [
        {
            "file_path": "a.py",
            "line": 10 + index,
            "category": categories[index],
            "severity": "high",
        }
        for index in range(label_count - 1)
    ]

    score = score_evaluation(_overlapping_suite(labels, observed))

    assert score.true_positives == label_count - 1
    assert score.false_positives == 0
    assert score.false_negatives == 1


def test_the_category_preference_never_costs_a_match() -> None:
    """Preferring an agreeing category must not starve a stricter label.

    The loose label would greedily take the exact-category observation that the
    tolerance-0 label is the only claimant for. Augmenting has to move it.
    """

    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "loose",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "line_tolerance": 3,
                },
                {
                    "finding_id": "strict",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "line_tolerance": 0,
                },
            ],
            [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "high",
                },
                {
                    "file_path": "a.py",
                    "line": 11,
                    "category": "correctness",
                    "severity": "high",
                },
            ],
        )
    )

    assert score.true_positives == 2
    assert score.false_positives == 0
    assert score.false_negatives == 0
    assert score.category_mismatches == 1


def test_a_severity_label_still_constrains_the_match() -> None:
    """A stated severity has to agree; an omitted one places no constraint.

    Stating a severity the reviewer disagreed with costs the label its match
    twice: the label is a false negative and the observation that located it is
    a false positive.
    """

    observed = {
        "file_path": "a.py",
        "line": 10,
        "category": "correctness",
        "severity": "low",
    }
    label = {
        "finding_id": "only",
        "file_path": "a.py",
        "line": 10,
        "category": "security",
    }

    gated = score_evaluation(_overlapping_suite([{**label, "severity": "critical"}], [observed]))
    ungated = score_evaluation(_overlapping_suite([label], [observed]))

    assert (gated.true_positives, gated.false_negatives, gated.false_positives) == (0, 1, 1)
    assert (ungated.true_positives, ungated.false_negatives, ungated.false_positives) == (1, 0, 0)


def test_a_severity_disagreement_is_named_rather_than_left_in_the_totals() -> None:
    """The double charge the severity gate imposes is reported, not just paid.

    A label the reviewer landed on and graded differently is counted as a false
    negative and a false positive, which in the totals alone is indistinguishable
    from a defect nobody noticed plus an unrelated finding.
    """

    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "graded",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "critical",
                }
            ],
            [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "high",
                }
            ],
        )
    )

    assert (score.false_negatives, score.false_positives) == (1, 1)
    assert score.severity_mismatches == 1
    assert [
        (item.expected.value, item.observed.value, item.count)
        for item in score.severity_confusion
    ] == [("critical", "high", 1)]
    assert score.cases[0].severity_mismatches[0].finding_id == "graded"


def test_a_label_with_no_severity_reports_no_severity_mismatch() -> None:
    score = score_evaluation(
        _overlapping_suite(
            [
                {
                    "finding_id": "ungraded",
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                }
            ],
            [
                {
                    "file_path": "a.py",
                    "line": 10,
                    "category": "security",
                    "severity": "low",
                }
            ],
        )
    )

    assert score.true_positives == 1
    assert score.severity_mismatches == 0


def test_the_category_confusion_table_does_not_depend_on_observed_order() -> None:
    """Which mismatch is reported must not be decided by list position.

    `CAPTURE.md` tells the capturer to reconsider a label when this table shows a
    repeated confusion, so a table that names `security` on one ordering and
    `performance` on the reverse would send them to edit the fixture on the
    strength of an artifact. Found by search against the previous tie-break,
    which fell through to the observation's index.
    """

    labels = [
        {
            "finding_id": "near",
            "file_path": "a.py",
            "line": 11,
            "category": "correctness",
            "line_tolerance": 0,
        },
        {
            "finding_id": "far",
            "file_path": "a.py",
            "line": 10,
            "category": "performance",
            "line_tolerance": 0,
        },
    ]
    security = {
        "file_path": "a.py",
        "line": 11,
        "category": "security",
        "severity": "high",
    }
    performance = {
        "file_path": "a.py",
        "line": 11,
        "category": "performance",
        "severity": "high",
    }

    forward = score_evaluation(_overlapping_suite(labels, [security, performance]))
    reverse = score_evaluation(_overlapping_suite(labels, [performance, security]))

    def table(score) -> list[tuple[str, str, str]]:
        return sorted(
            (mismatch.finding_id, mismatch.expected.value, mismatch.observed.value)
            for case in score.cases
            for mismatch in case.category_mismatches
        )

    assert table(forward) == table(reverse)
    assert (forward.true_positives, forward.false_positives, forward.false_negatives) == (
        reverse.true_positives,
        reverse.false_positives,
        reverse.false_negatives,
    )


def test_the_category_confusion_table_survives_every_observed_permutation() -> None:
    """Not just the two-element swap: every ordering has to agree."""

    labels = [
        {
            "finding_id": f"label-{index}",
            "file_path": "a.py",
            "line": 10 + index,
            "category": category,
            "line_tolerance": 1,
        }
        for index, category in enumerate(("correctness", "security", "performance"))
    ]
    observed = [
        {"file_path": "a.py", "line": line, "category": category, "severity": "high"}
        for line, category in ((10, "security"), (11, "performance"), (12, "security"))
    ]

    tables = {
        tuple(
            sorted(
                (mismatch.finding_id, mismatch.expected.value, mismatch.observed.value)
                for case in score_evaluation(
                    _overlapping_suite(labels, list(ordering))
                ).cases
                for mismatch in case.category_mismatches
            )
        )
        for ordering in itertools.permutations(observed)
    }

    assert len(tables) == 1


@pytest.mark.parametrize("tolerance", (11, 50))
def test_a_label_cannot_claim_more_of_a_file_than_the_ceiling(tolerance: int) -> None:
    """A span wide enough to swallow a whole function is not a location.

    Location is now the entire match gate, so `line_tolerance` is the only thing
    bounding how much of a file one label absorbs.
    """

    with pytest.raises(ValidationError):
        _overlapping_suite(
            [
                {
                    "finding_id": "wide",
                    "file_path": "a.py",
                    "line": 40,
                    "category": "security",
                    "line_tolerance": tolerance,
                }
            ],
            [],
        )

    assert MAX_LINE_TOLERANCE == 10


def test_a_suite_written_against_the_previous_schema_is_refused() -> None:
    """The narrowed tolerance ceiling makes some v1 suites invalid, so v1 ends.

    A `line_tolerance` of 50 was accepted under `diffuse-evaluation-v1` and is
    refused now. Silently reading such a file under the old version name would
    report a schema error about a field the author was entitled to use.
    """

    with pytest.raises(ValidationError):
        EvaluationSuite.model_validate(
            {
                "schema_version": "diffuse-evaluation-v1",
                "name": "old",
                "model": "test",
                "cases": [{"case_id": "one"}],
            }
        )


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
                "schema_version": "diffuse-evaluation-v2",
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
