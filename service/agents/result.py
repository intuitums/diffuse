"""Structured result and error contract for CLI-native agent sessions.

Diffuse validates this shape before any report assembly, persistence, or
GitHub publication. The runner may fail at a named stage; the control plane
keeps the audit reference and never echoes CLI/provider credentials.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from service.agents.capability import PROFILE_NAMES, RUNTIME_NAMES

AgentResultStage = Literal[
    "session_start",
    "tool_call",
    "structured_output",
    "session_end",
]

AGENT_RESULT_STAGES: frozenset[str] = frozenset(
    {
        "session_start",
        "tool_call",
        "structured_output",
        "session_end",
    }
)

#: Stable error codes the worker may surface without leaking vendor detail.
AGENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "capability_invalid",
        "capability_expired",
        "capability_budget_exhausted",
        "runtime_unavailable",
        "authentication_required",
        "session_timeout",
        "session_execution_failed",
        "structured_output_invalid",
        "tool_scope_denied",
        "rate_limited",
        "internal_error",
    }
)


class AgentSessionResult(BaseModel):
    """One completed or failed agent-runner session.

    Success carries a schema-validated payload under `value`. Failure carries
    `error_code` / `error_message` and the stage where the session stopped.
    Token counts from provider APIs are intentionally absent: CLI runtimes do
    not reliably expose them, so spend is recorded as turns and wall time.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    stage: AgentResultStage
    audit_ref: str = Field(min_length=1, max_length=128)
    runtime: Literal["claude", "codex"]
    profile: Literal["answer", "review", "verify", "learn"]
    capability_id: str = Field(min_length=1, max_length=128)
    value: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=1024)
    turns_used: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _consistent(self) -> AgentSessionResult:
        if self.runtime not in RUNTIME_NAMES:
            raise ValueError(f"runtime {self.runtime!r} is unsupported")
        if self.profile not in PROFILE_NAMES:
            raise ValueError(f"profile {self.profile!r} is unsupported")
        if self.stage not in AGENT_RESULT_STAGES:
            raise ValueError(f"stage {self.stage!r} is unsupported")
        if self.ok:
            if self.value is None:
                raise ValueError("successful agent results require value")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful agent results must not carry errors")
            return self
        if self.error_code is None:
            raise ValueError("failed agent results require error_code")
        if self.error_code not in AGENT_ERROR_CODES:
            raise ValueError(f"error_code {self.error_code!r} is unsupported")
        if self.error_message is None or not self.error_message.strip():
            raise ValueError("failed agent results require error_message")
        if self.value is not None:
            raise ValueError("failed agent results must not carry value")
        return self


def success_result(
    *,
    stage: AgentResultStage,
    audit_ref: str,
    runtime: Literal["claude", "codex"],
    profile: Literal["answer", "review", "verify", "learn"],
    capability_id: str,
    value: dict[str, Any],
    turns_used: int = 0,
    wall_time_seconds: float = 0.0,
) -> AgentSessionResult:
    return AgentSessionResult(
        ok=True,
        stage=stage,
        audit_ref=audit_ref,
        runtime=runtime,
        profile=profile,
        capability_id=capability_id,
        value=value,
        turns_used=turns_used,
        wall_time_seconds=wall_time_seconds,
    )


def failure_result(
    *,
    stage: AgentResultStage,
    audit_ref: str,
    runtime: Literal["claude", "codex"],
    profile: Literal["answer", "review", "verify", "learn"],
    capability_id: str,
    error_code: str,
    error_message: str,
    turns_used: int = 0,
    wall_time_seconds: float = 0.0,
) -> AgentSessionResult:
    return AgentSessionResult(
        ok=False,
        stage=stage,
        audit_ref=audit_ref,
        runtime=runtime,
        profile=profile,
        capability_id=capability_id,
        error_code=error_code,
        error_message=error_message,
        turns_used=turns_used,
        wall_time_seconds=wall_time_seconds,
    )
