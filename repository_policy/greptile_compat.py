"""Strict, deterministic import of the public root greptile.json contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import (
    ContextSettingsPatch,
    RepositoryConfig,
    RepositoryRule,
    validate_filter_pattern,
    validate_keyword,
    validate_repo_glob,
    validate_repo_path,
)


class GreptileCompatibilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GreptileOutputSection(GreptileCompatibilityModel):
    included: bool | None = None
    collapsible: bool | None = None
    defaultOpen: bool | None = None


class GreptileContext(GreptileCompatibilityModel):
    repos: tuple[str, ...] = Field(default=(), max_length=7)


class GreptileCustomRule(GreptileCompatibilityModel):
    rule: str = Field(min_length=1, max_length=6000)
    scope: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)

    @field_validator("scope")
    @classmethod
    def valid_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class GreptileCustomFile(GreptileCompatibilityModel):
    path: str = Field(min_length=1, max_length=1024)
    description: str | None = Field(default=None, max_length=500)
    scope: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return validate_repo_path(value)

    @field_validator("scope")
    @classmethod
    def valid_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class GreptileCustomOther(GreptileCompatibilityModel):
    content: str = Field(min_length=1, max_length=6000)
    scope: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)

    @field_validator("scope")
    @classmethod
    def valid_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class GreptileCustomContext(GreptileCompatibilityModel):
    rules: tuple[GreptileCustomRule, ...] = Field(default=(), max_length=100)
    files: tuple[GreptileCustomFile, ...] = Field(default=(), max_length=100)
    other: tuple[GreptileCustomOther, ...] = Field(default=(), max_length=100)


class GreptileConfig(GreptileCompatibilityModel):
    strictness: Literal[1, 2, 3] | None = None
    commentTypes: tuple[
        Literal["logic", "syntax", "style", "info"],
        ...,
    ] | None = Field(default=None, min_length=1, max_length=4)
    triggerOnUpdates: bool | None = None
    triggerOnDrafts: bool | None = None
    skipReview: Literal["AUTOMATIC"] | None = None
    labels: tuple[str, ...] | None = Field(default=None, max_length=100)
    disabledLabels: tuple[str, ...] | None = Field(default=None, max_length=100)
    includeAuthors: tuple[str, ...] | None = Field(default=None, max_length=100)
    excludeAuthors: tuple[str, ...] | None = Field(default=None, max_length=100)
    includeBranches: tuple[str, ...] | None = Field(default=None, max_length=100)
    excludeBranches: tuple[str, ...] | None = Field(default=None, max_length=100)
    includeKeywords: str | None = Field(default=None, max_length=20_000)
    ignoreKeywords: str | None = Field(default=None, max_length=20_000)
    fileChangeLimit: int | None = Field(default=None, ge=1, le=100_000)
    ignorePatterns: str | None = Field(default=None, max_length=100_000)
    context: GreptileContext | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=100_000)
    customContext: GreptileCustomContext | None = None
    patternRepositories: tuple[str, ...] | None = Field(
        default=None,
        max_length=7,
    )
    shouldUpdateDescription: bool | None = None
    updateSummaryOnly: bool | None = None
    fixWithAI: bool | None = None
    hideFooter: bool | None = None
    includeIssuesTable: bool | None = None
    includeConfidenceScore: bool | None = None
    includeSequenceDiagram: bool | None = None
    summarySection: GreptileOutputSection | None = None
    issuesTableSection: GreptileOutputSection | None = None
    confidenceScoreSection: GreptileOutputSection | None = None
    sequenceDiagramSection: GreptileOutputSection | None = None
    statusCheck: bool | None = None
    statusCommentsEnabled: bool | None = None

    @field_validator("commentTypes")
    @classmethod
    def unique_comment_types(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("commentTypes must be unique")
        return value

    @field_validator(
        "labels",
        "disabledLabels",
        "includeAuthors",
        "excludeAuthors",
        "includeBranches",
        "excludeBranches",
    )
    @classmethod
    def valid_filters(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_filter_pattern(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("filter values must be unique ignoring case")
        return normalized


@dataclass(frozen=True)
class ImportedInlineGuidance:
    source_path: str
    kind: Literal["instructions", "context"]
    applies_to: tuple[str, ...]
    content: str
    description: str | None
    priority: int


@dataclass(frozen=True)
class ImportedGreptilePolicy:
    config: RepositoryConfig
    inline_guidance: tuple[ImportedInlineGuidance, ...]
    context_files: tuple[GreptileCustomFile, ...]


def _newline_values(
    value: str | None,
    *,
    field_name: str,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = tuple(line.strip() for line in value.splitlines() if line.strip())
    if len(items) > 100:
        raise ValueError(f"{field_name} exceeds 100 entries")
    normalized = tuple(validate_keyword(item) for item in items)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ValueError(f"{field_name} values must be unique ignoring case")
    return normalized


def _ignore_patterns(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    patterns: list[str] = []
    for raw_line in value.splitlines():
        pattern = raw_line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.startswith("!"):
            raise ValueError(
                "greptile.json ignorePatterns negation is not supported; "
                "use native .diffuse path policy"
            )
        if "\\" in pattern or "[" in pattern or "]" in pattern:
            raise ValueError(
                "greptile.json ignorePatterns escapes and character classes "
                "are not supported; use native .diffuse path policy"
            )
        pattern = pattern.removeprefix("/")
        if "/" not in pattern.rstrip("/"):
            pattern = f"**/{pattern}"
        patterns.append(validate_repo_glob(pattern))
    if len(patterns) > 100:
        raise ValueError("greptile.json ignorePatterns exceeds 100 entries")
    if len(set(patterns)) != len(patterns):
        raise ValueError("greptile.json ignorePatterns contains duplicates")
    return tuple(patterns)


def _section(
    simple_included: bool | None,
    section: GreptileOutputSection | None,
) -> dict[str, bool]:
    values: dict[str, bool] = {}
    if simple_included is not None:
        values["included"] = simple_included
    if section is None:
        return values
    if section.included is not None:
        values["included"] = section.included
    if section.collapsible is not None:
        values["collapsible"] = section.collapsible
    if section.defaultOpen is not None:
        values["default_open"] = section.defaultOpen
    return values


def _comment_type_guidance(comment_types: tuple[str, ...]) -> str:
    rendered = ", ".join(comment_types)
    return (
        "Imported greptile.json commentTypes: "
        f"{rendered}. Limit optional review feedback to the selected classes: "
        "logic means concrete correctness or reliability defects; syntax means "
        "compile or parser defects; style means concrete maintainability violations; "
        "info means actionable architecture, API, testing, or performance findings. "
        "Do not suppress directly evidenced security defects."
    )


def import_greptile_config(config: GreptileConfig) -> ImportedGreptilePolicy:
    """Translate settings with equivalent behavior and reject unsafe lossy mappings."""
    custom = config.customContext or GreptileCustomContext()
    repositories = (
        *(config.context.repos if config.context is not None else ()),
        *(config.patternRepositories or ()),
    )
    context = ContextSettingsPatch(repos=repositories or None)

    severity_by_strictness = {
        1: "low",
        2: "medium",
        3: "high",
    }
    review = {
        "ignored_paths": _ignore_patterns(config.ignorePatterns),
        "summary_only": config.updateSummaryOnly,
        "hide_footer": config.hideFooter,
        "update_description": config.shouldUpdateDescription,
        "summary_comment": config.statusCommentsEnabled,
        "fix_with_agent": config.fixWithAI,
        "issues_table_section": _section(
            config.includeIssuesTable,
            config.issuesTableSection,
        ),
        "confidence_score_section": _section(
            config.includeConfidenceScore,
            config.confidenceScoreSection,
        ),
        "summary_section": _section(None, config.summarySection),
        "diagram": _section(
            config.includeSequenceDiagram,
            config.sequenceDiagramSection,
        ),
    }
    if config.strictness is not None:
        review["minimum_severity"] = severity_by_strictness[config.strictness]

    triggers = {
        "automatic": False if config.skipReview == "AUTOMATIC" else None,
        "review_drafts": config.triggerOnDrafts,
        "review_updates": config.triggerOnUpdates,
        "labels": config.labels,
        "disabled_labels": config.disabledLabels,
        "include_authors": config.includeAuthors,
        "exclude_authors": config.excludeAuthors,
        "include_branches": config.includeBranches,
        "exclude_branches": config.excludeBranches,
        "include_keywords": _newline_values(
            config.includeKeywords,
            field_name="includeKeywords",
        ),
        "exclude_keywords": _newline_values(
            config.ignoreKeywords,
            field_name="ignoreKeywords",
        ),
        "file_change_limit": config.fileChangeLimit,
        "status_check": config.statusCheck,
    }
    rules = tuple(
        RepositoryRule(
            id=f"greptile-rule-{index:03d}",
            title=f"Imported Greptile rule {index}",
            guidance=rule.rule,
            applies_to=rule.scope,
        )
        for index, rule in enumerate(custom.rules, start=1)
    )
    repository_config = RepositoryConfig.model_validate(
        {
            "version": 1,
            "review": review,
            "triggers": triggers,
            "context": context.model_dump(mode="json"),
            "rules": [rule.model_dump(mode="json") for rule in rules],
        }
    )

    inline_guidance: list[ImportedInlineGuidance] = []
    if config.instructions is not None:
        inline_guidance.append(
            ImportedInlineGuidance(
                source_path="greptile.json#instructions",
                kind="instructions",
                applies_to=("**",),
                content=config.instructions,
                description="Imported Greptile review instructions",
                priority=20,
            )
        )
    if config.commentTypes is not None:
        inline_guidance.append(
            ImportedInlineGuidance(
                source_path="greptile.json#commentTypes",
                kind="instructions",
                applies_to=("**",),
                content=_comment_type_guidance(config.commentTypes),
                description="Imported Greptile comment type selection",
                priority=20,
            )
        )
    inline_guidance.extend(
        ImportedInlineGuidance(
            source_path=f"greptile.json#customContext.other[{index}]",
            kind="context",
            applies_to=value.scope,
            content=value.content,
            description="Imported Greptile custom context",
            priority=0,
        )
        for index, value in enumerate(custom.other)
    )
    return ImportedGreptilePolicy(
        config=repository_config,
        inline_guidance=tuple(inline_guidance),
        context_files=custom.files,
    )
