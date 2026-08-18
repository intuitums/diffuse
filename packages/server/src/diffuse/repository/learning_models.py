"""Strict records for feedback-derived, human-approved review rules."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from diffuse.repository.policy.models import (
    CategoryName,
    SeverityName,
    validate_repo_glob,
)
from diffuse.repository.scm import validate_repository_name

FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuleLearningJobEvent:
    repository_id: int
    repo_full_name: str
    generation: int
    evidence_fingerprint: str

    def __post_init__(self) -> None:
        if self.repository_id <= 0 or self.generation <= 0:
            raise ValueError("Rule-learning identity must be positive")
        object.__setattr__(
            self,
            "repo_full_name",
            validate_repository_name(self.repo_full_name),
        )
        if not FINGERPRINT_PATTERN.fullmatch(self.evidence_fingerprint):
            raise ValueError("Rule-learning evidence fingerprint is invalid")

    @property
    def scope_key(self) -> str:
        return f"repository:{self.repo_full_name}:rule_learning"

    @property
    def idempotency_key(self) -> str:
        return (
            f"{self.scope_key}:generation:{self.generation}:"
            f"evidence:{self.evidence_fingerprint}"
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "repo_full_name": self.repo_full_name,
            "generation": self.generation,
            "evidence_fingerprint": self.evidence_fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuleLearningJobEvent:
        expected = {
            "repository_id",
            "repo_full_name",
            "generation",
            "evidence_fingerprint",
        }
        if set(payload) != expected:
            raise ValueError("Workflow payload does not match the rule-learning schema")
        repository_id = payload["repository_id"]
        generation = payload["generation"]
        if (
            not isinstance(repository_id, int)
            or isinstance(repository_id, bool)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
        ):
            raise ValueError("Rule-learning workflow identity has an invalid type")
        return cls(
            repository_id=repository_id,
            repo_full_name=str(payload["repo_full_name"]),
            generation=generation,
            evidence_fingerprint=str(payload["evidence_fingerprint"]),
        )


@dataclass(frozen=True)
class RuleLearningEvidence:
    event_id: int
    pull_request_id: int
    pull_request_number: int
    source_kind: str
    signal_kind: str
    content: str | None
    finding_title: str
    finding_body: str
    file_path: str
    category: str
    severity: str
    suppression_protected: bool
    security_classification: str | None = None


def evidence_fingerprint(evidence: tuple[RuleLearningEvidence, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "event_id": item.event_id,
                "pull_request_id": item.pull_request_id,
                "source_kind": item.source_kind,
                "signal_kind": item.signal_kind,
            }
            for item in evidence
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class StrictLearningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SuggestedRuleCandidate(StrictLearningModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    guidance: Annotated[str, Field(min_length=1, max_length=6000)]
    applies_to: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)
    severity: SeverityName = "medium"
    category: CategoryName = "maintainability"
    evidence_event_ids: tuple[int, ...] = Field(min_length=1, max_length=30)

    @field_validator("applies_to")
    @classmethod
    def valid_applies_to(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_repo_glob(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("learned-rule scopes must be unique")
        return normalized

    @field_validator("evidence_event_ids")
    @classmethod
    def valid_evidence_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("learned-rule evidence IDs must be unique and positive")
        return value


class SuggestedRuleBatch(StrictLearningModel):
    suggestions: tuple[SuggestedRuleCandidate, ...] = Field(default=(), max_length=20)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def candidate_similarity_text(candidate: SuggestedRuleCandidate) -> str:
    return f"{_normalized_text(candidate.title)} {_normalized_text(candidate.guidance)}"


def candidate_deduplication_key(candidate: SuggestedRuleCandidate) -> str:
    canonical = json.dumps(
        {
            "title": _normalized_text(candidate.title),
            "guidance": _normalized_text(candidate.guidance),
            "applies_to": sorted(candidate.applies_to),
            "severity": candidate.severity,
            "category": candidate.category,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class RuleLearningWork:
    run_id: int | None
    status: str
    evidence: tuple[RuleLearningEvidence, ...]
    evidence_fingerprint: str

    @property
    def needs_generation(self) -> bool:
        return self.status == "generating"


@dataclass(frozen=True)
class RuleLearningResult:
    proposed: int
    consolidated: int
    rejected_candidates: int


@dataclass(frozen=True)
class LearnedRuleRecord:
    id: int
    repository_id: int
    status: str
    version: int
    title: str
    guidance: str
    applies_to: tuple[str, ...]
    severity: str
    category: str
    evidence_count: int
