"""Deterministic scoring for labeled Diffuse review evaluation sets."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from service.models.review import Category, Severity


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: Widest line span a single label may claim, as a half-width in lines.
#:
#: A label names a place as well as a defect. Textual overlap now prevents an
#: entirely unrelated finding in that place from being credited, but a wide
#: span still makes it easier for a vaguely related observation to collide with
#: the label. Ten lines each way is a 21-line window -- about one function body,
#: and an order of magnitude wider than any committed fixture, every one of
#: which uses 1.
MAX_LINE_TOLERANCE = 10

TITLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
# Tokens that can occur in almost any finding title and therefore provide no
# evidence that two titles describe the same defect. Keep this deliberately
# small: title matching is a guard against coincidental location matches, not a
# semantic search engine, and aggressive stop-wording would create false misses.
TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "bug",
        "can",
        "could",
        "defect",
        "does",
        "finding",
        "for",
        "from",
        "in",
        "is",
        "issue",
        "it",
        "may",
        "not",
        "of",
        "on",
        "or",
        "problem",
        "that",
        "the",
        "this",
        "to",
        "when",
        "with",
    }
)


def _title_tokens(title: str) -> frozenset[str]:
    """Return normalized title terms useful for deterministic overlap."""

    return frozenset(
        token
        for token in TITLE_TOKEN_PATTERN.findall(title.casefold())
        if token not in TITLE_STOP_WORDS
    )


def _titles_overlap(expected: str, observed: str) -> bool:
    return bool(_title_tokens(expected) & _title_tokens(observed))


class ExpectedFinding(EvaluationModel):
    finding_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    file_path: str = Field(min_length=1, max_length=1024)
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"]
    category: Category
    severity: Severity | None = None
    line_tolerance: int = Field(default=3, ge=0, le=MAX_LINE_TOLERANCE)

    @field_validator("title")
    @classmethod
    def title_has_a_matchable_token(cls, title: str) -> str:
        if not _title_tokens(title):
            raise ValueError("expected finding title must contain a non-generic token")
        return title


class ObservedFinding(EvaluationModel):
    title: str = Field(min_length=1, max_length=200)
    file_path: str = Field(min_length=1, max_length=1024)
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"]
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
    #: Hash of the fixture files this case was produced from. Pins the *content*
    #: the run was scored against, which the label count alone does not: making
    #: a bug more obvious in `diff.patch` raises recall without the review
    #: engine changing at all, and the baseline would still pass.
    fixture_digest: str | None = Field(default=None, min_length=1, max_length=128)

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


class ReviewDepthRendering(EvaluationModel):
    """What one review stage was actually sent for the requested depth.

    Requesting a depth and receiving it are different things: a route can accept
    `reasoning_effort` and render it as a boolean, or refuse it outright. A
    suite that recorded only what was *asked* would let a baseline captured at
    `thorough` be defended by a run that sent no reasoning parameter at all --
    which is what happens on `openai/gpt-4.1-mini`, the model
    `evals/CAPTURE.md` recommends capturing on first.
    """

    stage: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=512)
    #: `service.model_capabilities.ReasoningMechanism`, as its value.
    mechanism: str = Field(min_length=1, max_length=64)
    #: The effort rung actually sent, or None when nothing was sent.
    effort: str | None = Field(default=None, min_length=1, max_length=64)


class RunConfiguration(EvaluationModel):
    """Everything besides the model names that moves the score.

    The model name was already pinned; none of these were, and every one of
    them changes what a run finds. Raising `MIN_REVIEW_CONFIDENCE` to 0.99 for
    one capture records a baseline with near-zero recall and near-zero false
    positives that every later run clears trivially, forever. Dropping a review
    pass, or bumping `PROMPT_VERSION`, does the same in the other direction.
    `service/cli/review.py` already treats a `PROMPT_VERSION` change as
    invalidating a stored run; a baseline is a stored run that outlives many more
    of them.
    """

    prompt_version: str = Field(min_length=1, max_length=200)
    min_review_confidence: float = Field(ge=0, le=1)
    review_passes: list[str] = Field(min_length=1, max_length=32)
    #: The depth that was requested, or None when none was.
    requested_review_depth: str | None = Field(default=None, min_length=1, max_length=64)
    #: What each stage will actually be sent. Empty when no depth was requested.
    depth_renderings: list[ReviewDepthRendering] = Field(
        default_factory=list, max_length=8
    )

    def differences(self, other: RunConfiguration) -> list[str]:
        """Every field on which `self` and `other` disagree, in words."""

        differences: list[str] = []
        for label, mine, theirs in (
            ("PROMPT_VERSION", self.prompt_version, other.prompt_version),
            (
                "MIN_REVIEW_CONFIDENCE",
                self.min_review_confidence,
                other.min_review_confidence,
            ),
            ("REVIEW_PASSES", ",".join(self.review_passes), ",".join(other.review_passes)),
            (
                "review depth",
                self.requested_review_depth or "unset",
                other.requested_review_depth or "unset",
            ),
        ):
            if mine != theirs:
                differences.append(f"{label} was {theirs!r} and is now {mine!r}")
        mine_rendered = {
            rendering.stage: rendering for rendering in self.depth_renderings
        }
        theirs_rendered = {
            rendering.stage: rendering for rendering in other.depth_renderings
        }
        for stage in sorted(mine_rendered.keys() | theirs_rendered.keys()):
            mine_stage = mine_rendered.get(stage)
            theirs_stage = theirs_rendered.get(stage)
            if mine_stage == theirs_stage:
                continue
            differences.append(
                f"the {stage} stage was sent {_render_summary(theirs_stage)} and is "
                f"now sent {_render_summary(mine_stage)}"
            )
        return differences


def _render_summary(rendering: ReviewDepthRendering | None) -> str:
    if rendering is None:
        return "nothing (the stage did not run)"
    if rendering.effort is None:
        return f"no reasoning parameter by {rendering.model}"
    return f"reasoning_effort={rendering.effort} ({rendering.mechanism}) by {rendering.model}"


class EvaluationSuite(EvaluationModel):
    #: Bumped from v2 because expected and observed findings now require title
    #: text and diff side. A v2 suite cannot be scored honestly under the new
    #: match gate because neither signal was recorded.
    schema_version: str = Field(pattern=r"^diffuse-evaluation-v3$")
    name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=512)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    verifier_model: str | None = Field(default=None, min_length=1, max_length=512)
    verifier_pricing: ModelPricing | None = None
    #: Present on a suite the harness produced; absent on one written by hand
    #: from a reviewed pull request, which has no run to describe.
    run_configuration: RunConfiguration | None = None
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


class SeverityMismatch(EvaluationModel):
    """A label the reviewer landed on and graded differently, so it did not match.

    Unlike `CategoryMismatch` this one *is* a penalty, and an expensive one: the
    severity gate charges the label as a false negative **and** the observation
    that found it as a false positive -- the same double charge DEV-292 removed
    from category. The gate stays, because a label opts into severity by stating
    one and category offers no such opt-out, but the cost was previously
    invisible in the score. This makes it legible: every entry here is one
    finding paid for twice.
    """

    finding_id: str
    expected: Severity
    observed: Severity


class SeverityConfusion(EvaluationModel):
    """How often one labeled severity was reported as another, suite-wide."""

    expected: Severity
    observed: Severity
    count: int


class CaseScore(EvaluationModel):
    case_id: str
    true_positives: int
    false_positives: int
    false_negatives: int
    addressed_findings: int
    latency_ms: int
    category_mismatches: list[CategoryMismatch] = Field(default_factory=list)
    severity_mismatches: list[SeverityMismatch] = Field(default_factory=list)
    fixture_digest: str | None = None


class EvaluationScore(EvaluationModel):
    schema_version: str = "diffuse-evaluation-score-v5"
    suite_name: str
    model: str
    verifier_model: str | None
    #: Echoed from the suite so a baseline can be captured and compared from the
    #: score alone, exactly as `model` and `verifier_model` already are.
    run_configuration: RunConfiguration | None = None
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
    #: Labels the reviewer landed on but graded differently. Unlike
    #: `category_mismatches` these are *not* a subset of `true_positives`: each
    #: one is already counted as a false negative and a false positive.
    severity_mismatches: int = 0
    severity_confusion: list[SeverityConfusion] = Field(default_factory=list)
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

    The gate is the same file and diff side, a line within the label's own
    tolerance, and at least one normalized non-generic title token in common.
    Requiring text prevents a style nit beside a real injection from earning
    credit for the injection merely because both landed in the same small diff.

    Category is deliberately *not* part of the gate. A label's category is
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

    The gate is not free, and the cost is the same double charge category used
    to impose: a severity disagreement makes the label a false negative *and*
    the observation that found it a false positive. `SeverityMismatch` reports
    each one so that cost is visible in the score rather than silently folded
    into it. Do not state a severity on a label unless the grade is part of the
    claim.

    Nothing here lets two labels share an observation, or one label absorb two:
    `_score_case` matches injectively, so the count of true positives can never
    exceed the number of distinct observations, however generous the tolerance.
    """

    return (
        expected.file_path == observed.file_path
        and expected.side == observed.side
        and abs(expected.line - observed.line) <= expected.line_tolerance
        and _titles_overlap(expected.title, observed.title)
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
    # Adjacency is ordered by (category disagreement, line distance, observed
    # category, observed line), which keeps the chosen assignment stable across
    # runs and across reorderings of the observed list. Category leads that key
    # so that when a label can be satisfied either by an observation that agrees
    # on category or by one that does not, the agreeing one is *preferred*.
    #
    # Preferred, not guaranteed. This is a greedy-local preference, not a
    # solution to the min-cost assignment problem: minimising reported
    # mismatches over all maximum matchings is a different problem, and an
    # augmenting path can displace a label off an observation it already agreed
    # with. Measured over 6,000 random cases the reported table was non-minimal
    # in 572 of them. Ordering within a label's adjacency cannot change the
    # *size* of a maximum matching, so precision, recall and F1 do not depend on
    # this preference; only which maximum matching is chosen, and therefore the
    # diagnostics, do -- which is why `category_mismatches` is recorded in a
    # baseline and reported as a delta rather than gated on.
    #
    # The final tie-break is the observation's own (category, line) rather than
    # its position in the list. With the index there, the same inputs in a
    # different observed order produced a different confusion table -- unstable
    # in 1,089 of those 6,000 cases. Two observations that tie on all of
    # (category agreement, line distance, category, line) are indistinguishable
    # to this table, so the remaining index tie-break cannot move it and is
    # present only to keep the sort total.
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
                case.observed[index].category.value,
                case.observed[index].line,
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
        severity_mismatches=_severity_mismatches(
            case,
            matched_expected=set(observed_to_expected.values()),
            unmatched_observed=unmatched_observed,
        ),
        fixture_digest=case.fixture_digest,
    )


def _severity_mismatches(
    case: EvaluationCase,
    *,
    matched_expected: set[int],
    unmatched_observed: set[int],
) -> list[SeverityMismatch]:
    """Name the labels the severity gate turned into a double charge.

    Reads the matching; never changes it. A label with a stated severity that
    went unmatched, sitting on top of an observation that agrees on file, side,
    line span, and title overlap but disagrees only on severity, is one finding
    charged as a false negative and a false positive at once. That cost is real
    and is policy -- stating a
    severity is an explicit claim that a review grading the defect differently
    has not really found it -- but it was previously indistinguishable in the
    score from a defect nobody noticed.

    Labels are visited by `finding_id` and observations ranked by their own
    (distance, severity, line), so the table does not depend on the order either
    list happens to be written in.
    """

    unmatched_expected = sorted(
        (
            index
            for index in range(len(case.expected))
            if index not in matched_expected and case.expected[index].severity is not None
        ),
        key=lambda index: case.expected[index].finding_id,
    )
    if not unmatched_expected:
        return []
    available = set(unmatched_observed)
    reported: list[SeverityMismatch] = []
    for expected_index in unmatched_expected:
        expected = case.expected[expected_index]
        candidates = sorted(
            (
                index
                for index in available
                if case.observed[index].file_path == expected.file_path
                and case.observed[index].side == expected.side
                and abs(expected.line - case.observed[index].line) <= expected.line_tolerance
                and _titles_overlap(expected.title, case.observed[index].title)
                and case.observed[index].severity is not expected.severity
            ),
            key=lambda index: (
                abs(expected.line - case.observed[index].line),
                case.observed[index].severity.value,
                case.observed[index].line,
            ),
        )
        if not candidates:
            continue
        chosen = candidates[0]
        available.discard(chosen)
        reported.append(
            SeverityMismatch(
                finding_id=expected.finding_id,
                # `severity` is not None: unmatched_expected filtered on it.
                expected=expected.severity,  # type: ignore[arg-type]
                observed=case.observed[chosen].severity,
            )
        )
    return reported


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
    # Reported beside the quality metrics too, but for the opposite reason:
    # these are already *inside* precision and recall, twice over, and the
    # score alone cannot tell them apart from a defect the reviewer missed.
    severity_counts: dict[tuple[Severity, Severity], int] = defaultdict(int)
    for case in cases:
        for severity_mismatch in case.severity_mismatches:
            severity_counts[
                (severity_mismatch.expected, severity_mismatch.observed)
            ] += 1
    severity_confusion = [
        SeverityConfusion(expected=expected, observed=observed, count=count)
        for (expected, observed), count in sorted(
            severity_counts.items(),
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
        run_configuration=suite.run_configuration,
        case_count=len(suite.cases),
        expected_finding_count=sum(len(case.expected) for case in suite.cases),
        observed_finding_count=sum(len(case.observed) for case in suite.cases),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        addressed_findings=sum(case.addressed_findings for case in cases),
        category_mismatches=sum(len(case.category_mismatches) for case in cases),
        category_confusion=category_confusion,
        severity_mismatches=sum(len(case.severity_mismatches) for case in cases),
        severity_confusion=severity_confusion,
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
