"""Backend-neutral contracts for schema-constrained model generation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from service.model_config import ModelTarget


class ModelExecutionError(RuntimeError):
    """Base class for failures normalized at the executor boundary."""

    code = "model_execution_failed"
    retryable = True


class ExecutorUnavailableError(ModelExecutionError):
    code = "executor_unavailable"


class ExecutorNotAuthenticatedError(ModelExecutionError):
    code = "executor_not_authenticated"
    retryable = False


class ExecutorVersionUnsupportedError(ModelExecutionError):
    code = "executor_version_unsupported"
    retryable = False


class ModelRequestTimeoutError(ModelExecutionError):
    code = "model_request_timeout"


class ModelRequestCancelledError(ModelExecutionError):
    code = "model_request_cancelled"
    retryable = False


class ModelRateLimitedError(ModelExecutionError):
    code = "model_rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StructuredOutputInvalidError(ModelExecutionError):
    code = "structured_output_invalid"

    def __init__(self, message: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


@dataclass(frozen=True)
class StructuredGenerationRequest[T: BaseModel]:
    request_id: str
    workload: str
    response_model: type[T]
    system_prompt: str
    user_prompt: str
    target: ModelTarget
    max_output_tokens: int
    timeout_seconds: int
    idempotency_key: str
    cancellation_check: Callable[[], bool] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 255:
            raise ValueError("Structured generation request_id is invalid")
        if not self.workload or len(self.workload) > 64:
            raise ValueError("Structured generation workload is invalid")
        if self.max_output_tokens <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Structured generation limits must be positive")
        if len(self.system_prompt.encode()) > 262_144:
            raise ValueError("Structured generation system prompt is too large")
        if len(self.user_prompt.encode()) > 4_194_304:
            raise ValueError("Structured generation user prompt is too large")

    @property
    def fingerprint(self) -> str:
        payload = {
            "workload": self.workload,
            "response_schema": self.response_model.model_json_schema(),
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "target": self.target.to_dict(),
            "max_output_tokens": self.max_output_tokens,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class StructuredGenerationResult[T: BaseModel]:
    value: T
    prompt_tokens: int = 0
    completion_tokens: int = 0
    resolved_model: str | None = None
    executor_version: str | None = None
    finish_reason: str | None = None
    metadata: dict[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("Structured generation token counts cannot be negative")
        encoded = json.dumps(self.metadata, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 16_384:
            raise ValueError("Structured generation metadata is too large")


class StructuredGenerator(Protocol):
    def generate[T: BaseModel](
        self,
        request: StructuredGenerationRequest[T],
    ) -> StructuredGenerationResult[T]:
        """Generate one schema-valid value or raise a normalized failure."""


@dataclass(frozen=True)
class StoredGenerationStep:
    response: dict[str, object]
    prompt_tokens: int
    completion_tokens: int
    resolved_model: str | None = None
    executor_version: str | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("Stored generation token counts cannot be negative")


class GenerationStepStore(Protocol):
    """Durable cache controlled by a workflow, not by a model adapter."""

    def load(
        self,
        *,
        step_key: str,
        request_fingerprint: str,
    ) -> StoredGenerationStep | None:
        """Return a completed matching step, or None."""

    def save(
        self,
        *,
        step_key: str,
        request_fingerprint: str,
        response_schema: str,
        result: StructuredGenerationResult[BaseModel],
    ) -> None:
        """Persist one already-validated step."""
