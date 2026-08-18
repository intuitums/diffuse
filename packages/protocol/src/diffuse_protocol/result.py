"""Schema-validated structured results from an agent-host session.

The runner returns findings through this contract. Diffuse alone validates the
payload, applies policy, and publishes as the GitHub App. Validation failures
are classified by a stable taxonomy so workers can decide retry vs terminal
without parsing free-form CLI stderr.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from diffuse_protocol.investigation import DEFAULT_MAX_RESULT_BYTES
from diffuse_protocol.review import (
    BodyText,
    Category,
    PathText,
    SecurityClassification,
    Severity,
    ShortText,
    StrictModel,
    VerificationDecision,
)

RESULT_SCHEMA_VERSION = 1
VERIFICATION_RESULT_SCHEMA_VERSION = 1

#: Host-added transport fields. They travel with the result envelope but are
#: not part of the CLI investigation contract.
RESULT_ENVELOPE_KEYS = frozenset(
    {"session_id", "capability_id", "prompt_tokens", "completion_tokens"}
)


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
    RESULT_TOO_LARGE = "result_too_large"
    AUDIT_REFERENCE_MISMATCH = "audit_reference_mismatch"
    CANDIDATE_RESULT_DIGEST_MISMATCH = "candidate_result_digest_mismatch"
    UNKNOWN_DECISION_ID = "unknown_decision_id"
    DUPLICATE_DECISION_ID = "duplicate_decision_id"


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
    """The schema-validated candidate payload an agent-host may return."""

    schema_version: Literal[1] = RESULT_SCHEMA_VERSION
    runtime: Literal["claude", "codex"]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    risk_score: float = Field(ge=0, le=10)
    findings: list[AgentFinding] = Field(default_factory=list, max_length=80)
    audit_reference: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Must equal the investigation session_id that produced this result.",
        ),
    ]
    coverage_caveat: Annotated[str | None, Field(max_length=2000)] = None


class AgentVerificationResult(StrictModel):
    """The schema-validated verifier payload an agent-host may return."""

    schema_version: Literal[1] = VERIFICATION_RESULT_SCHEMA_VERSION
    runtime: Literal["claude", "codex"]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    risk_score: float = Field(ge=0, le=10)
    decisions: list[VerificationDecision] = Field(default_factory=list, max_length=80)
    candidate_result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    audit_reference: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description=(
                "Must equal the verifier investigation session_id that produced "
                "this result."
            ),
        ),
    ]
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


def _loaded_result_payload(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
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
    return loaded


def _validate_result_model(
    loaded: dict[str, Any],
    *,
    version: int,
    model: type[AgentInvestigationResult] | type[AgentVerificationResult],
) -> AgentInvestigationResult | AgentVerificationResult:
    schema_version = loaded.get("schema_version")
    if schema_version is not None and schema_version != version:
        raise ResultValidationError(
            f"unsupported agent session result schema_version {schema_version!r}",
            code=ResultValidationFailureCode.UNSUPPORTED_VERSION,
            field="schema_version",
        )
    try:
        return model.model_validate(loaded)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or None
        code = _classify_pydantic_error(first)
        raise ResultValidationError(
            first.get("msg", "agent session result failed schema validation"),
            code=code,
            field=location,
        ) from error


def validate_agent_investigation_result(
    payload: str | bytes | dict[str, Any],
) -> AgentInvestigationResult:
    """Parse and validate a candidate runner result."""

    result = _validate_result_model(
        _loaded_result_payload(payload),
        version=RESULT_SCHEMA_VERSION,
        model=AgentInvestigationResult,
    )
    assert isinstance(result, AgentInvestigationResult)
    if not result.findings and not result.summary.strip():
        raise ResultValidationError(
            "agent session result has no findings and no summary",
            code=ResultValidationFailureCode.EMPTY_FINDINGS_WITHOUT_SUMMARY,
        )
    return result


def validate_agent_verification_result(
    payload: str | bytes | dict[str, Any],
) -> AgentVerificationResult:
    """Parse and validate a verifier runner result."""

    result = _validate_result_model(
        _loaded_result_payload(payload),
        version=VERIFICATION_RESULT_SCHEMA_VERSION,
        model=AgentVerificationResult,
    )
    assert isinstance(result, AgentVerificationResult)
    return result


def canonical_result_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Stable transport bytes for result hashing and durable replay checks."""

    if not isinstance(payload, dict):
        raise ResultValidationError(
            "agent session result must be a JSON object",
            code=ResultValidationFailureCode.NOT_AN_OBJECT,
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def canonical_result_payload_digest(payload: dict[str, Any]) -> str:
    """Stable SHA-256 of one canonical result payload."""

    return hashlib.sha256(canonical_result_payload_bytes(payload)).hexdigest()


def _accept_bound_result(
    payload: dict[str, Any],
    *,
    session_id: str,
    runtime: str,
    max_result_bytes: int,
    validator,
) -> AgentInvestigationResult | AgentVerificationResult:
    encoded = canonical_result_payload_bytes(payload)
    if len(encoded) > max_result_bytes:
        raise ResultValidationError(
            f"agent session result exceeds {max_result_bytes} bytes",
            code=ResultValidationFailureCode.RESULT_TOO_LARGE,
        )

    contract_payload = {
        key: value for key, value in payload.items() if key not in RESULT_ENVELOPE_KEYS
    }
    result = validator(contract_payload)
    if result.runtime != runtime:
        raise ResultValidationError(
            "agent session result runtime did not match the selected runtime",
            code=ResultValidationFailureCode.CONSTRAINT_VIOLATION,
            field="runtime",
        )
    if result.audit_reference != session_id:
        raise ResultValidationError(
            "agent session result audit_reference must equal the investigation session_id",
            code=ResultValidationFailureCode.AUDIT_REFERENCE_MISMATCH,
            field="audit_reference",
        )
    return result


def accept_bound_agent_investigation_result(
    payload: dict[str, Any],
    *,
    session_id: str,
    runtime: str,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> AgentInvestigationResult:
    """Validate a candidate runner result and bind it to one investigation."""

    result = _accept_bound_result(
        payload,
        session_id=session_id,
        runtime=runtime,
        max_result_bytes=max_result_bytes,
        validator=validate_agent_investigation_result,
    )
    assert isinstance(result, AgentInvestigationResult)
    return result


def accept_candidate_result_input(
    payload: dict[str, Any],
    *,
    expected_digest: str | None = None,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> tuple[AgentInvestigationResult, str]:
    """Validate one accepted candidate payload before a verifier consumes it."""

    candidate_session_id = payload.get("session_id")
    candidate_runtime = payload.get("runtime")
    if not isinstance(candidate_session_id, str) or not isinstance(candidate_runtime, str):
        raise ResultValidationError(
            "candidate verification input must include session_id and runtime",
            code=ResultValidationFailureCode.MISSING_FIELD,
            field="session_id",
        )
    result = accept_bound_agent_investigation_result(
        payload,
        session_id=candidate_session_id,
        runtime=candidate_runtime,
        max_result_bytes=max_result_bytes,
    )
    digest = canonical_result_payload_digest(payload)
    if expected_digest is not None and digest != expected_digest:
        raise ResultValidationError(
            "candidate verification input digest did not match the dispatched digest",
            code=ResultValidationFailureCode.CANDIDATE_RESULT_DIGEST_MISMATCH,
            field="candidate_result_digest",
        )
    return result, digest


def accept_bound_agent_verification_result(
    payload: dict[str, Any],
    *,
    session_id: str,
    runtime: str,
    candidate_result_digest: str,
    allowed_candidate_ids: Collection[str],
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> AgentVerificationResult:
    """Validate a verifier runner result and bind it to one investigation."""

    result = _accept_bound_result(
        payload,
        session_id=session_id,
        runtime=runtime,
        max_result_bytes=max_result_bytes,
        validator=validate_agent_verification_result,
    )
    assert isinstance(result, AgentVerificationResult)
    if result.candidate_result_digest != candidate_result_digest:
        raise ResultValidationError(
            "verifier result candidate_result_digest did not match the dispatched digest",
            code=ResultValidationFailureCode.CANDIDATE_RESULT_DIGEST_MISMATCH,
            field="candidate_result_digest",
        )
    allowed = frozenset(allowed_candidate_ids)
    seen: set[str] = set()
    for decision in result.decisions:
        if decision.candidate_id not in allowed:
            raise ResultValidationError(
                f"verifier result decision references unknown {decision.candidate_id}",
                code=ResultValidationFailureCode.UNKNOWN_DECISION_ID,
                field="decisions.candidate_id",
            )
        if decision.candidate_id in seen:
            raise ResultValidationError(
                f"verifier result decision repeats {decision.candidate_id}",
                code=ResultValidationFailureCode.DUPLICATE_DECISION_ID,
                field="decisions.candidate_id",
            )
        seen.add(decision.candidate_id)
    return result
