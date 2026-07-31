"""Deterministic scoring for labeled Diffuse review evaluation sets."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from service.review_models import Category, Severity


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: Widest line span a single label may claim, as a half-width in lines.
#:
#: A label names a *place*, and location is now the whole match gate (see
#: `_matches`), so this ceiling is the only thing bounding how much of a file
#: one label can absorb. Ten lines each way is a 21-line window -- about one
#: function body, and already wider than any committed fixture, which use 3 to
#: 5. The previous ceiling of 50 spanned 101 lines: past that a label has
#: stopped identifying a place and started claiming a region, and any finding
#: anywhere in it would be credited as having found the labeled defect.
MAX_LINE_TOLERANCE = 10


class ExpectedFinding(EvaluationModel):
    finding_id: str = Field(min_length=1, max_length=200)
    file_path: str = Field(min_length=1, max_length=1024)
    line: int = Field(gt=0)
    category: Category
    severity: Severity | None = None
    line_tolerance: int = Field(default=3, ge=0, le=MAX_LINE_TOLERANCE)


class ObservedFinding(EvaluationModel):
    file_path: str = Field(min_length=1, max_length=1024)
    line: int = Field(gt=0)
    category: Category
    severity: Severity
    fingerprint: str | None = Field(default=None, max_length=200)


class EvaluationCase(EvaluationModel):
    case_id: str = Field(min_length=1, max_length=200)
    expected: list[ExpectedFinding] = Field(default_factory=list, max_length=200)
    observed: list[ObservedFinding] = Field(default_factory=list, max_length=200)
    addressed_finding_ids: list[str] = Field(default_factory=list, max_length=200)
    latency_ms: int = Field(default=0, ge=0)
    # Candidate-generation tokens. Verification runs against a separately
    # configured model whose rates need not match, so its tokens are counted and
    # priced on their own rather than folded in here.
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    verifier_prompt_tokens: int = Field(default=0, ge=0)
    verifier_completion_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def addressed_findings_are_labeled(self) -> EvaluationCase:
        expected_ids = {finding.finding_id for finding in self.expected}
        if len(expected_ids) != len(self.expected):
            raise ValueError("expected finding_id values must be unique within a case")
        if not set(self.addressed_finding_ids).issubset(expected_ids):
            raise ValueError("addressed_finding_ids must refer to expected findings")
        return self


class ModelPricing(EvaluationModel):
    input_usd_per_million_tokens: float = Field(default=0, ge=0)
    output_usd_per_million_tokens: float = Field(default=0, ge=0)


class EvaluationSuite(EvaluationModel):
    schema_version: str = Field(pattern=r"^diffuse-evaluation-v1$")
    name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=512)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    verifier_model: str | None = Field(default=None, min_length=1, max_length=512)
    verifier_pricing: ModelPricing | None = None
    cases: list[EvaluationCase] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> EvaluationSuite:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique")
        return self

    @model_validator(mode="after")
    def verifier_tokens_are_priced(self) -> EvaluationSuite:
        """Refuse a suite whose recorded cost would be silently wrong.

        A cross-family pair — the configuration provenance routing exists to
        use — bills candidate and verification tokens at two different rates.
        Applying one rate to both produces an `estimated_cost_usd` that looks
        authoritative and is not, so the label must state the second rate rather
        than let the suite guess it.
        """

        verifier_tokens = any(
            case.verifier_prompt_tokens or case.verifier_completion_tokens
            for case in self.cases
        )
        if verifier_tokens and self.verifier_model is None:
            raise ValueError("verifier token counts require a verifier_model")
        if self.verifier_pricing is not None and self.verifier_model is None:
            raise ValueError("verifier_pricing requires a verifier_model")
        if (
            self.verifier_model is not None
            and self.verifier_model != self.model
            and self.verifier_pricing is None
        ):
            raise ValueError(
                "a verifier_model that differs from the candidate model requires "
                "verifier_pricing"
            )
        return self

    @property
    def resolved_verifier_pricing(self) -> ModelPricing:
        """Rates for verification tokens; the candidate's when the model is shared."""

        return self.verifier_pricing or self.pricing


class CategoryMismatch(EvaluationModel):
    """A finding that was found, and filed under a different category.

    This is a diagnostic, not a penalty: the pair it describes is counted as one
    true positive. It exists because the disagreement is genuinely worth knowing
    -- a reviewer that consistently files injections as `correctness` is telling
    you something about its prompt -- and because folding it into the match
    decision, as this scorer used to, charged the same finding as a false
    negative *and* a false positive.
    """

    finding_id: str
    expected: Category
    observed: Category


class CategoryConfusion(EvaluationModel):
    """How often one labeled category was reported as another, suite-wide."""

    expected: Category
    observed: Category
    count: int


class CaseScore(EvaluationModel):
    case_id: str
    true_positives: int
    false_positives: int
    false_negatives: int
    addressed_findings: int
    latency_ms: int
    category_mismatches: list[CategoryMismatch] = Field(default_factory=list)


class EvaluationScore(EvaluationModel):
    schema_version: str = "diffuse-evaluation-score-v3"
    suite_name: str
    model: str
    verifier_model: str | None
    case_count: int
    expected_finding_count: int
    observed_finding_count: int
    true_positives: int
    false_positives: int
    false_negatives: int
    addressed_findings: int
    #: True positives whose category disagreed with the label. A subset of
    #: `true_positives`, reported beside precision/recall rather than inside it.
    category_mismatches: int = 0
    category_confusion: list[CategoryConfusion] = Field(default_factory=list)
    precision: float
    recall: float
    f1: float
    median_latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    candidate_prompt_tokens: int
    candidate_completion_tokens: int
    verifier_prompt_tokens: int
    verifier_completion_tokens: int
    estimated_cost_usd: float
    cases: list[CaseScore]


def _matches(expected: ExpectedFinding, observed: ObservedFinding) -> bool:
    """Is this observation about the defect this label describes?

    The gate is **location**: the same file, and a line within the label's own
    tolerance. Category is deliberately *not* part of it. A label's category is
    mandatory, so a labeller cannot opt out of it, and requiring equality meant
    a model that found a real SQL injection and filed it under `correctness`
    scored strictly worse than a model that missed the bug entirely -- the same
    false negative either way, plus a false positive for the near miss. Being
    right in the wrong taxonomy was punished harder than being wrong. The
    disagreement is still reported, as `CategoryMismatch`, where it informs
    without corrupting precision, recall or F1.

    Severity stays a gate, and the asymmetry is deliberate: `severity` is
    optional and defaults to unset, so a label only opts into it by stating one,
    and stating one is an explicit claim that a review calling this defect minor
    has not really found it. `category` offers no such opt-out.

    Nothing here lets two labels share an observation, or one label absorb two:
    `_score_case` matches injectively, so the count of true positives can never
    exceed the number of distinct observations, however generous the tolerance.
    """

    return (
        expected.file_path == observed.file_path
        and abs(expected.line - observed.line) <= expected.line_tolerance
        and (expected.severity is None or expected.severity is observed.severity)
    )


def _score_case(case: EvaluationCase) -> CaseScore:
    # Maximum-cardinality bipartite matching between labels and observations.
    #
    # Taking each label's nearest free observation in list order is not the same
    # thing: a label processed early can consume the only observation a later,
    # stricter label could have matched, and that starved label is then charged
    # as both a false negative and a false positive. Two labels a few lines
    # apart in one file are enough to trigger it at the default line tolerance,
    # so the score would depend on the order labels happen to be written in.
    #
    # `_matches` requires equality on file path, so the graph decomposes into
    # one independent bucket per file and each augmenting search stays small.
    # Adjacency is ordered by (category disagreement, line distance, observation
    # index), which keeps the chosen assignment stable across runs and across
    # reorderings of the observed list. Category leads that key so that when a
    # label can be satisfied either by an observation that agrees on category or
    # by one that does not, the agreeing one is taken and no mismatch is
    # reported for what was only ever an assignment artifact. Ordering within a
    # label's adjacency cannot change the *size* of a maximum matching, so
    # precision, recall and F1 do not depend on this preference; only which
    # maximum matching is chosen, and therefore the diagnostics, do.
    #
    # Maximum cardinality alone does not pin down *which* labels get matched
    # when two labels compete for one observation, and `addressed_findings`
    # counts matched labels. Visiting labels in list order therefore let a
    # reordering of `expected` change the reported addressed count with
    # identical labels and observations. Labels named in
    # `addressed_finding_ids` are visited first instead: augmenting never
    # unmatches an already-matched label, and matchable label sets form a
    # transversal matroid, so this both preserves maximum cardinality and
    # maximises the addressed count over every maximum matching. The
    # tie-break is the label's identity rather than its position, so list
    # order no longer decides the score.
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, observed in enumerate(case.observed):
        buckets[observed.file_path].append(index)

    adjacency: list[list[int]] = []
    for expected in case.expected:
        candidates = [
            index
            for index in buckets.get(expected.file_path, ())
            if _matches(expected, case.observed[index])
        ]
        candidates.sort(
            key=lambda index: (
                case.observed[index].category is not expected.category,
                abs(expected.line - case.observed[index].line),
                index,
            )
        )
        adjacency.append(candidates)

    observed_to_expected: dict[int, int] = {}

    def _augment(expected_index: int, visited: set[int]) -> bool:
        for observed_index in adjacency[expected_index]:
            if observed_index in visited:
                continue
            visited.add(observed_index)
            holder = observed_to_expected.get(observed_index)
            if holder is None or _augment(holder, visited):
                observed_to_expected[observed_index] = expected_index
                return True
        return False

    addressed_ids = set(case.addressed_finding_ids)
    visit_order = sorted(
        range(len(case.expected)),
        key=lambda index: (
            case.expected[index].finding_id not in addressed_ids,
            index,
        ),
    )
    for expected_index in visit_order:
        _augment(expected_index, set())

    true_positives = len(observed_to_expected)
    matched_ids = {
        case.expected[expected_index].finding_id
        for expected_index in observed_to_expected.values()
    }
    unmatched_observed = set(range(len(case.observed))) - observed_to_expected.keys()
    mismatches = [
        CategoryMismatch(
            finding_id=case.expected[expected_index].finding_id,
            expected=case.expected[expected_index].category,
            observed=case.observed[observed_index].category,
        )
        for observed_index, expected_index in sorted(observed_to_expected.items())
        if case.observed[observed_index].category
        is not case.expected[expected_index].category
    ]
    mismatches.sort(key=lambda mismatch: mismatch.finding_id)
    return CaseScore(
        case_id=case.case_id,
        true_positives=true_positives,
        false_positives=len(unmatched_observed),
        false_negatives=len(case.expected) - true_positives,
        addressed_findings=len(matched_ids.intersection(case.addressed_finding_ids)),
        latency_ms=case.latency_ms,
        category_mismatches=mismatches,
    )


def score_evaluation(suite: EvaluationSuite) -> EvaluationScore:
    cases = [_score_case(case) for case in suite.cases]
    true_positives = sum(case.true_positives for case in cases)
    false_positives = sum(case.false_positives for case in cases)
    false_negatives = sum(case.false_negatives for case in cases)
    precision = (
        true_positives / (true_positives + false_positives)
        if true_positives + false_positives
        else 1.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if true_positives + false_negatives
        else 1.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    # Reported beside the quality metrics, never inside them. A systematic
    # confusion -- security consistently filed as correctness, say -- is a
    # prompt problem worth seeing, and it is invisible if it is only ever
    # aggregated into a lower recall.
    confusion_counts: dict[tuple[Category, Category], int] = defaultdict(int)
    for case in cases:
        for mismatch in case.category_mismatches:
            confusion_counts[(mismatch.expected, mismatch.observed)] += 1
    category_confusion = [
        CategoryConfusion(expected=expected, observed=observed, count=count)
        for (expected, observed), count in sorted(
            confusion_counts.items(),
            key=lambda item: (-item[1], item[0][0].value, item[0][1].value),
        )
    ]
    latencies = sorted(case.latency_ms for case in suite.cases)
    middle = len(latencies) // 2
    median_latency = (
        latencies[middle]
        if len(latencies) % 2
        else round((latencies[middle - 1] + latencies[middle]) / 2)
    )
    candidate_prompt_tokens = sum(case.prompt_tokens for case in suite.cases)
    candidate_completion_tokens = sum(case.completion_tokens for case in suite.cases)
    verifier_prompt_tokens = sum(case.verifier_prompt_tokens for case in suite.cases)
    verifier_completion_tokens = sum(
        case.verifier_completion_tokens for case in suite.cases
    )
    verifier_pricing = suite.resolved_verifier_pricing
    cost = (
        candidate_prompt_tokens * suite.pricing.input_usd_per_million_tokens
        + candidate_completion_tokens * suite.pricing.output_usd_per_million_tokens
        + verifier_prompt_tokens * verifier_pricing.input_usd_per_million_tokens
        + verifier_completion_tokens * verifier_pricing.output_usd_per_million_tokens
    ) / 1_000_000
    return EvaluationScore(
        suite_name=suite.name,
        model=suite.model,
        verifier_model=suite.verifier_model,
        case_count=len(suite.cases),
        expected_finding_count=sum(len(case.expected) for case in suite.cases),
        observed_finding_count=sum(len(case.observed) for case in suite.cases),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        addressed_findings=sum(case.addressed_findings for case in cases),
        category_mismatches=sum(len(case.category_mismatches) for case in cases),
        category_confusion=category_confusion,
        precision=precision,
        recall=recall,
        f1=f1,
        median_latency_ms=median_latency,
        prompt_tokens=candidate_prompt_tokens + verifier_prompt_tokens,
        completion_tokens=candidate_completion_tokens + verifier_completion_tokens,
        candidate_prompt_tokens=candidate_prompt_tokens,
        candidate_completion_tokens=candidate_completion_tokens,
        verifier_prompt_tokens=verifier_prompt_tokens,
        verifier_completion_tokens=verifier_completion_tokens,
        estimated_cost_usd=cost,
        cases=cases,
    )
