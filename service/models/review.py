"""Versioned structured models for native Diffuse reviews."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    TESTING = "testing"
    ARCHITECTURE = "architecture"
    MAINTAINABILITY = "maintainability"
    API = "api"


class SecurityClassification(StrEnum):
    VULNERABILITY = "vulnerability"
    PREVENTATIVE = "preventative"


class DiagramKind(StrEnum):
    SEQUENCE = "sequence"
    ENTITY_RELATION = "entity_relation"
    CLASS = "class"
    FLOW = "flow"


ShortText = Annotated[str, Field(min_length=1, max_length=200)]
BodyText = Annotated[str, Field(min_length=1, max_length=4000)]
PathText = Annotated[str, Field(min_length=1, max_length=1024)]


class CandidateFinding(StrictModel):
    title: ShortText
    body: BodyText
    severity: Severity
    category: Category
    security_classification: SecurityClassification | None = None
    confidence: float = Field(ge=0, le=1)
    file_path: PathText
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    evidence: Annotated[str, Field(min_length=1, max_length=3000)]
    suggested_fix: Annotated[str | None, Field(max_length=6000)] = None

    @model_validator(mode="after")
    def classification_matches_category(self) -> CandidateFinding:
        if self.category is Category.SECURITY:
            if self.security_classification is None:
                self.security_classification = SecurityClassification.VULNERABILITY
        elif self.security_classification is not None:
            raise ValueError(
                "security_classification is allowed only for security findings"
            )
        return self


class CandidateBatch(StrictModel):
    analysis_summary: Annotated[str, Field(min_length=1, max_length=2000)]
    findings: list[CandidateFinding] = Field(default_factory=list, max_length=30)


class VerificationDecision(StrictModel):
    candidate_id: Annotated[str, Field(pattern=r"^candidate-[0-9]+$")]
    keep: bool
    confidence: float = Field(ge=0, le=1)
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    revised_title: Annotated[str | None, Field(max_length=200)] = None
    revised_body: Annotated[str | None, Field(max_length=4000)] = None
    revised_suggested_fix: Annotated[str | None, Field(max_length=6000)] = None
    revised_severity: Severity | None = None


class VerificationBatch(StrictModel):
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    risk_score: float = Field(ge=0, le=10)
    decisions: list[VerificationDecision] = Field(default_factory=list, max_length=80)


class ReviewDiagram(StrictModel):
    kind: DiagramKind
    title: Annotated[str, Field(min_length=1, max_length=120)]
    mermaid: Annotated[str, Field(min_length=1, max_length=12_000)]

    @model_validator(mode="after")
    def safe_supported_mermaid(self) -> ReviewDiagram:
        source = self.mermaid.replace("\r\n", "\n").replace("\r", "\n").strip()
        lines = source.splitlines()
        if (
            len(lines) > 200
            or any(len(line) > 500 for line in lines)
            or any(ord(character) < 32 for character in self.title)
            or any(
                ord(character) < 32 and character not in {"\n", "\t"}
                for character in source
            )
        ):
            raise ValueError("diagram source exceeds safe structural limits")
        lowered = source.casefold()
        if (
            "```" in source
            or "%%{" in source
            or "javascript:" in lowered
            or "data:" in lowered
            or "http://" in lowered
            or "https://" in lowered
            or re.search(
                r"(?im)^\s*(?:click|style|classDef|linkStyle)\b|"
                r"\bhref\s*=|<(?:script|iframe|img|style)\b",
                source,
            )
        ):
            raise ValueError("diagram source contains an unsafe directive")
        first = next(
            (line.strip() for line in lines if line.strip() and not line.lstrip().startswith("%%")),
            "",
        )
        valid_directive = {
            DiagramKind.SEQUENCE: first == "sequenceDiagram",
            DiagramKind.ENTITY_RELATION: first == "erDiagram",
            DiagramKind.CLASS: first == "classDiagram",
            DiagramKind.FLOW: bool(
                re.fullmatch(r"(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)", first)
            ),
        }[self.kind]
        if not valid_directive:
            raise ValueError("diagram kind does not match its Mermaid directive")
        self.mermaid = source
        return self


class DiagramProposal(StrictModel):
    diagram: ReviewDiagram | None = None


class ReviewFinding(StrictModel):
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    title: ShortText
    body: BodyText
    severity: Severity
    category: Category
    security_classification: SecurityClassification | None = None
    confidence: float = Field(ge=0, le=1)
    file_path: PathText
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"]
    evidence: Annotated[str, Field(min_length=1, max_length=3000)]
    suggested_fix: Annotated[str | None, Field(max_length=6000)] = None

    @model_validator(mode="after")
    def classification_matches_category(self) -> ReviewFinding:
        if self.category is Category.SECURITY:
            if self.security_classification is None:
                self.security_classification = SecurityClassification.VULNERABILITY
        elif self.security_classification is not None:
            raise ValueError(
                "security_classification is allowed only for security findings"
            )
        return self


class ReviewReport(StrictModel):
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    risk_score: float = Field(ge=0, le=10)
    confidence_score: int = Field(default=5, ge=0, le=5)
    diagram: ReviewDiagram | None = None
    diagram_collapsible: bool = True
    diagram_default_open: bool = True
    summary_section_included: bool = True
    summary_section_collapsible: bool = False
    summary_section_default_open: bool = True
    issues_table_section_included: bool = True
    issues_table_section_collapsible: bool = False
    issues_table_section_default_open: bool = True
    confidence_score_section_included: bool = True
    confidence_score_section_collapsible: bool = False
    confidence_score_section_default_open: bool = True
    footer_included: bool = True
    update_description: bool = False
    summary_comment_enabled: bool = True
    fix_with_agent_enabled: bool = True
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=50)
    diff_file_count: int = Field(ge=0)
    reviewed_file_count: int = Field(ge=0)
    ignored_file_count: int = Field(default=0, ge=0)
    inline_comments_enabled: bool = True
    publication_enabled: bool = True
    skip_reason: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_]{1,64}$",
    )
    context_chunk_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    # Candidate and verifier stages can use different models. Keep their usage
    # separate so evaluation can price a cross-family review correctly without
    # inspecting a runtime's private call path.
    #
    # `None` means "this runtime did not report a split", which is not the same
    # claim as zero and must not be quietly rounded into one. A consumer that
    # subtracts the verifier share from the total would otherwise price a
    # cross-family review entirely on the candidate's rate card and produce a
    # number that looks authoritative and is wrong -- the exact failure the
    # harness used to avoid by observing each call. A runtime that genuinely ran
    # no verification stage reports 0 and says so.
    verifier_prompt_tokens: int | None = Field(default=None, ge=0)
    verifier_completion_tokens: int | None = Field(default=None, ge=0)
    # A breakdown of `prompt_tokens`, never an addition to it. LiteLLM folds both
    # cache counters into `prompt_tokens` on the Anthropic route, unlike
    # Anthropic's own `input_tokens`, which reports the uncached remainder alone;
    # adding all three together double-counts every cached review. Cache reads
    # bill at about a tenth of the base input rate and writes at 1.25x, so these
    # two are what says whether a review's prompt tokens were cheap or full price
    # -- `prompt_tokens` alone cannot distinguish them.
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def reports_verifier_usage(self) -> bool:
        """Whether this report can be priced per stage at all."""

        return (
            self.verifier_prompt_tokens is not None
            and self.verifier_completion_tokens is not None
        )

    @model_validator(mode="after")
    def verifier_usage_is_part_of_total_usage(self) -> ReviewReport:
        if (self.verifier_prompt_tokens is None) != (
            self.verifier_completion_tokens is None
        ):
            raise ValueError(
                "Verifier prompt and completion usage must both be reported or both be absent"
            )
        if (self.verifier_prompt_tokens or 0) > self.prompt_tokens or (
            self.verifier_completion_tokens or 0
        ) > self.completion_tokens:
            raise ValueError("Verifier token usage cannot exceed total review usage")
        return self
