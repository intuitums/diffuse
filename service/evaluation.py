"""Deterministic scoring for labeled Diffuse review evaluation sets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from service.review_models import Category, Severity


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExpectedFinding(EvaluationModel):
    finding_id: str = Field(min_length=1, max_length=200)
    file_path: str = Field(min_length=1, max_length=1024)
    line: int = Field(gt=0)
    category: Category
    severity: Severity | None = None
    line_tolerance: int = Field(default=3, ge=0, le=50)


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
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

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
    cases: list[EvaluationCase] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> EvaluationSuite:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique")
        return self


class CaseScore(EvaluationModel):
    case_id: str
    true_positives: int
    false_positives: int
    false_negatives: int
    addressed_findings: int
    latency_ms: int


class EvaluationScore(EvaluationModel):
    schema_version: str = "diffuse-evaluation-score-v1"
    suite_name: str
    model: str
    case_count: int
    expected_finding_count: int
    observed_finding_count: int
    true_positives: int
    false_positives: int
    false_negatives: int
    addressed_findings: int
    precision: float
    recall: float
    f1: float
    median_latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    cases: list[CaseScore]


def _matches(expected: ExpectedFinding, observed: ObservedFinding) -> bool:
    return (
        expected.file_path == observed.file_path
        and expected.category is observed.category
        and abs(expected.line - observed.line) <= expected.line_tolerance
        and (expected.severity is None or expected.severity is observed.severity)
    )


def _score_case(case: EvaluationCase) -> CaseScore:
    unmatched_observed = set(range(len(case.observed)))
    true_positives = 0
    matched_ids: set[str] = set()
    # Prefer the closest same-path/category match so one observed finding can
    # never receive credit for multiple labels.
    for expected in case.expected:
        candidates = [
            index
            for index in unmatched_observed
            if _matches(expected, case.observed[index])
        ]
        if not candidates:
            continue
        selected = min(
            candidates,
            key=lambda index: abs(expected.line - case.observed[index].line),
        )
        unmatched_observed.remove(selected)
        matched_ids.add(expected.finding_id)
        true_positives += 1
    return CaseScore(
        case_id=case.case_id,
        true_positives=true_positives,
        false_positives=len(unmatched_observed),
        false_negatives=len(case.expected) - true_positives,
        addressed_findings=len(matched_ids.intersection(case.addressed_finding_ids)),
        latency_ms=case.latency_ms,
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
    latencies = sorted(case.latency_ms for case in suite.cases)
    middle = len(latencies) // 2
    median_latency = (
        latencies[middle]
        if len(latencies) % 2
        else round((latencies[middle - 1] + latencies[middle]) / 2)
    )
    prompt_tokens = sum(case.prompt_tokens for case in suite.cases)
    completion_tokens = sum(case.completion_tokens for case in suite.cases)
    cost = (
        prompt_tokens * suite.pricing.input_usd_per_million_tokens
        + completion_tokens * suite.pricing.output_usd_per_million_tokens
    ) / 1_000_000
    return EvaluationScore(
        suite_name=suite.name,
        model=suite.model,
        case_count=len(suite.cases),
        expected_finding_count=sum(len(case.expected) for case in suite.cases),
        observed_finding_count=sum(len(case.observed) for case in suite.cases),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        addressed_findings=sum(case.addressed_findings for case in cases),
        precision=precision,
        recall=recall,
        f1=f1,
        median_latency_ms=median_latency,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
        cases=cases,
    )
