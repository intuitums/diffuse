"""Versioned wire models for the local Diffuse model runner."""

from __future__ import annotations

import json
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from service.model_config import RUNNER_PROTOCOL_VERSION

MAX_RUNNER_REQUEST_BYTES = 5 * 1024 * 1024
MAX_RUNNER_RESPONSE_BYTES = 2 * 1024 * 1024


class RunnerWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunnerGenerateRequest(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    operation: Literal["generate"] = "generate"
    request_id: Annotated[str, Field(min_length=1, max_length=255)]
    executor: Literal["codex-cli", "claude-cli"]
    model: Annotated[str, Field(min_length=1, max_length=512)]
    workload: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    system_prompt: str
    user_prompt: str
    output_schema: dict[str, object]
    max_output_tokens: int = Field(gt=0, le=200_000)
    deadline_unix_ms: int = Field(gt=0)
    # These fixed values make the security boundary inspectable on both ends.
    content_profile: Literal["diffuse_prepared_content_v1"] = "diffuse_prepared_content_v1"
    workspace_access: Literal["none"] = "none"
    publication_authority: Literal[False] = False

    @model_validator(mode="after")
    def bounded_content(self) -> RunnerGenerateRequest:
        if len(self.system_prompt.encode()) > 262_144:
            raise ValueError("runner system prompt is too large")
        if len(self.user_prompt.encode()) > 4_194_304:
            raise ValueError("runner user prompt is too large")
        schema = json.dumps(self.output_schema, separators=(",", ":")).encode()
        if len(schema) > 128 * 1024:
            raise ValueError("runner output schema is too large")
        if self.deadline_unix_ms <= int(time.time() * 1000):
            raise ValueError("runner request deadline has elapsed")
        return self


class RunnerHealthRequest(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    operation: Literal["health"] = "health"
    request_id: Annotated[str, Field(min_length=1, max_length=255)]


class RunnerCancelRequest(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    operation: Literal["cancel"] = "cancel"
    request_id: Annotated[str, Field(min_length=1, max_length=255)]
    target_request_id: Annotated[str, Field(min_length=1, max_length=255)]


class RunnerCancelSuccess(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    request_id: str
    status: Literal["ok"] = "ok"
    cancelled: bool


class RunnerGenerateSuccess(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    request_id: str
    status: Literal["ok"] = "ok"
    value: dict[str, object]
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    resolved_model: str | None = Field(default=None, max_length=512)
    executor_version: str | None = Field(default=None, max_length=255)
    finish_reason: str | None = Field(default=None, max_length=128)


class RunnerError(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    request_id: str
    status: Literal["error"] = "error"
    error_code: Literal[
        "executor_unavailable",
        "executor_not_authenticated",
        "executor_version_unsupported",
        "model_request_timeout",
        "model_request_cancelled",
        "model_rate_limited",
        "structured_output_invalid",
        "runner_busy",
        "runner_protocol_error",
        "runner_internal_error",
    ]
    retryable: bool
    retry_after_seconds: float | None = Field(default=None, ge=0, le=86_400)
    # Deliberately bounded and generic: raw CLI stderr never crosses the socket.
    message: str = Field(min_length=1, max_length=500)


class RunnerHealthSuccess(RunnerWireModel):
    protocol_version: Literal["diffuse-model-runner-v1"] = RUNNER_PROTOCOL_VERSION
    request_id: str
    status: Literal["ok"] = "ok"
    runner_version: str = Field(min_length=1, max_length=255)
    supported_executors: list[Literal["codex-cli", "claude-cli"]]
    capacity: int = Field(gt=0, le=128)
