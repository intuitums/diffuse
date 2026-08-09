"""Schema-validated structured results from an agent-host session.

The runner returns findings through this contract. Diffuse alone validates the
payload, applies policy, and publishes as the GitHub App. Validation failures
are classified by a stable taxonomy so workers can decide retry vs terminal
without parsing free-form CLI stderr.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from service.models.review import (
    BodyText,
    Category,
    PathText,
    SecurityClassification,
    Severity,
    ShortText,
    StrictModel,
)

RESULT_SCHEMA_VERSION = 1


class ResultValidationFailureCode(StrEnum):
    """Stable taxonomy for structured-result validation failures."""

    INVALID_JSON = "invalid_json"
    NOT_AN_OBJECT = "not_an_object"
    UNSUPPORTED_VERSION = "unsupported_version"
    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    TYPE_ERROR = "type_error"
    CONSTRAINT_VIOLATION = "constraint_violation"
    EMPTY_FINDINGS_WITHOUT_SUMMARY = "empty_findings_without_summary"
    SCHEMA_MISMATCH = "schema_mismatch"


class ResultValidationError(ValueError):
    """A structured agent result failed schema validation."""

    def __init__(
        self,
        message: str,
        *,
        code: ResultValidationFailureCode,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class AgentFinding(StrictModel):
    """One read-only finding produced by an agent session."""

    title: ShortText
    body: BodyText
    severity: Severity
    category: Category
    confidence: float = Field(ge=0, le=1)
    file_path: PathText
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    evidence: Annotated[str, Field(min_length=1, max_length=3000)]
    suggested_fix: Annotated[str | None, Field(max_length=6000)] = None
    security_classification: SecurityClassification | None = None

    @model_validator(mode="after")
    def classification_matches_category(self) -> AgentFinding:
        """Keep runner findings compatible with Diffuse's review finding contract."""

        if self.category is Category.SECURITY:
            if self.security_classification is None:
                self.security_classification = SecurityClassification.VULNERABILITY
        elif self.security_classification is not None:
            raise ValueError(
                "security_classification is allowed only for security findings"
            )
        return self


class AgentInvestigationResult(StrictModel):
    """The schema-validated payload an agent-host must return."""

    schema_version: Literal[1] = RESULT_SCHEMA_VERSION
    runtime: Literal["claude", "codex"]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    risk_score: float = Field(ge=0, le=10)
    findings: list[AgentFinding] = Field(default_factory=list, max_length=80)
    audit_reference: Annotated[str, Field(min_length=1, max_length=200)]
    coverage_caveat: Annotated[str | None, Field(max_length=2000)] = None


def _classify_pydantic_error(error: dict[str, Any]) -> ResultValidationFailureCode:
    error_type = str(error.get("type", ""))
    if error_type == "missing":
        return ResultValidationFailureCode.MISSING_FIELD
    if error_type in {"extra_forbidden", "unexpected_keyword_argument"}:
        return ResultValidationFailureCode.EXTRA_FIELD
    if (
        error_type.endswith("_type")
        or error_type.endswith("_parsing")
        or error_type in {
            "string_type",
            "int_type",
            "float_type",
            "bool_type",
            "list_type",
            "dict_type",
            "literal_error",
            "enum",
        }
    ):
        return ResultValidationFailureCode.TYPE_ERROR
    if error_type in {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "string_too_short",
        "string_too_long",
        "too_short",
        "too_long",
        "value_error",
    }:
        return ResultValidationFailureCode.CONSTRAINT_VIOLATION
    return ResultValidationFailureCode.SCHEMA_MISMATCH


def validate_agent_investigation_result(payload: str | bytes | dict[str, Any]) -> AgentInvestigationResult:
    """Parse and validate a runner result, raising a taxonomy-coded error on failure."""

    if isinstance(payload, (str, bytes)):
        try:
            loaded: Any = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
            raise ResultValidationError(
                f"agent session result is not valid JSON: {detail}",
                code=ResultValidationFailureCode.INVALID_JSON,
            ) from error
    else:
        loaded = payload

    if not isinstance(loaded, dict):
        raise ResultValidationError(
            "agent session result must be a JSON object",
            code=ResultValidationFailureCode.NOT_AN_OBJECT,
        )

    version = loaded.get("schema_version")
    if version is not None and version != RESULT_SCHEMA_VERSION:
        raise ResultValidationError(
            f"unsupported agent session result schema_version {version!r}",
            code=ResultValidationFailureCode.UNSUPPORTED_VERSION,
            field="schema_version",
        )

    try:
        result = AgentInvestigationResult.model_validate(loaded)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or None
        code = _classify_pydantic_error(first)
        raise ResultValidationError(
            first.get("msg", "agent session result failed schema validation"),
            code=code,
            field=location,
        ) from error

    if not result.findings and not result.summary.strip():
        raise ResultValidationError(
            "agent session result has no findings and no summary",
            code=ResultValidationFailureCode.EMPTY_FINDINGS_WITHOUT_SUMMARY,
        )
    return result
