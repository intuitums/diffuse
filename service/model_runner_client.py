"""Synchronous Unix-socket client for schema-constrained CLI generation."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from pydantic import ValidationError

from service.model_config import ModelExecutor
from service.model_execution import (
    ExecutorNotAuthenticatedError,
    ExecutorUnavailableError,
    ExecutorVersionUnsupportedError,
    ModelRateLimitedError,
    ModelRequestCancelledError,
    ModelRequestTimeoutError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    StructuredGenerator,
    StructuredOutputInvalidError,
)
from service.model_runner_protocol import (
    MAX_RUNNER_RESPONSE_BYTES,
    RunnerCancelRequest,
    RunnerCancelSuccess,
    RunnerError,
    RunnerGenerateRequest,
    RunnerGenerateSuccess,
    RunnerHealthRequest,
    RunnerHealthSuccess,
)


def _exchange(
    socket_path: Path,
    payload: bytes,
    *,
    timeout_seconds: float,
    cancellation_check=None,
    cancellation_request_id: str | None = None,
) -> bytes:
    if not socket_path.is_absolute():
        raise ValueError("Model runner socket path must be absolute")
    if b"\n" in payload:
        raise ValueError("Model runner payload must be one JSON line")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            deadline = time.monotonic() + timeout_seconds
            connection.settimeout(min(timeout_seconds, 0.5))
            connection.connect(str(socket_path))
            connection.sendall(payload + b"\n")
            chunks: list[bytes] = []
            size = 0
            while True:
                try:
                    chunk = connection.recv(min(65_536, MAX_RUNNER_RESPONSE_BYTES + 1 - size))
                except TimeoutError:
                    if cancellation_check is not None and cancellation_check():
                        if cancellation_request_id is not None:
                            cancel_runner_request(
                                socket_path,
                                cancellation_request_id,
                            )
                        raise ModelRequestCancelledError(
                            "Diffuse cancelled the active CLI request"
                        ) from None
                    if time.monotonic() >= deadline:
                        raise ModelRequestTimeoutError(
                            "Model runner request exceeded its deadline"
                        ) from None
                    continue
                if not chunk:
                    raise ExecutorUnavailableError("Model runner closed without a response")
                newline = chunk.find(b"\n")
                if newline >= 0:
                    chunks.append(chunk[:newline])
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RUNNER_RESPONSE_BYTES:
                    raise ExecutorUnavailableError("Model runner response exceeded its limit")
            return b"".join(chunks)
    except (ModelRequestCancelledError, ModelRequestTimeoutError):
        raise
    except (TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError) as error:
        raise ExecutorUnavailableError("Local model runner is unavailable") from error


def cancel_runner_request(socket_path: Path, target_request_id: str) -> bool:
    request = RunnerCancelRequest(
        request_id=f"cancel-{time.monotonic_ns()}",
        target_request_id=target_request_id,
    )
    try:
        response = _exchange(
            socket_path,
            request.model_dump_json().encode(),
            timeout_seconds=2,
        )
        return RunnerCancelSuccess.model_validate_json(response).cancelled
    except (ExecutorUnavailableError, ValidationError):
        return False


def runner_health(socket_path: Path, *, timeout_seconds: float = 2) -> RunnerHealthSuccess:
    request = RunnerHealthRequest(request_id=f"health-{time.monotonic_ns()}")
    response = _exchange(
        socket_path,
        request.model_dump_json().encode(),
        timeout_seconds=timeout_seconds,
    )
    try:
        parsed = json.loads(response)
        if parsed.get("status") == "error":
            error = RunnerError.model_validate(parsed)
            raise ExecutorUnavailableError(error.message)
        return RunnerHealthSuccess.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError, AttributeError) as error:
        raise ExecutorUnavailableError(
            "Model runner returned an invalid health response"
        ) from error


class RunnerStructuredGenerator(StructuredGenerator):
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def generate(self, request: StructuredGenerationRequest):
        if request.target.executor not in {
            ModelExecutor.CODEX_CLI,
            ModelExecutor.CLAUDE_CLI,
        }:
            raise ValueError("Runner generator requires a CLI target")
        wire_request = RunnerGenerateRequest(
            request_id=request.request_id,
            executor=request.target.executor.value,
            model=request.target.requested_model,
            workload=request.workload,
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            output_schema=request.response_model.model_json_schema(),
            max_output_tokens=request.max_output_tokens,
            deadline_unix_ms=int((time.time() + request.timeout_seconds) * 1000),
        )
        response = _exchange(
            self.socket_path,
            wire_request.model_dump_json().encode(),
            timeout_seconds=request.timeout_seconds + 2,
            cancellation_check=request.cancellation_check,
            cancellation_request_id=request.request_id,
        )
        try:
            parsed = json.loads(response)
            if parsed.get("status") == "error":
                self._raise_runner_error(RunnerError.model_validate(parsed))
            success = RunnerGenerateSuccess.model_validate(parsed)
            value = request.response_model.model_validate(success.value)
        except StructuredOutputInvalidError:
            raise
        except (json.JSONDecodeError, ValidationError, AttributeError) as error:
            raise StructuredOutputInvalidError(
                "Model runner returned invalid structured output",
                prompt_tokens=0,
                completion_tokens=0,
            ) from error
        return StructuredGenerationResult(
            value=value,
            prompt_tokens=success.prompt_tokens,
            completion_tokens=success.completion_tokens,
            resolved_model=success.resolved_model,
            executor_version=success.executor_version,
            finish_reason=success.finish_reason,
        )

    @staticmethod
    def _raise_runner_error(error: RunnerError) -> None:
        if error.error_code == "executor_not_authenticated":
            raise ExecutorNotAuthenticatedError(error.message)
        if error.error_code == "executor_version_unsupported":
            raise ExecutorVersionUnsupportedError(error.message)
        if error.error_code == "model_request_timeout":
            raise ModelRequestTimeoutError(error.message)
        if error.error_code == "model_request_cancelled":
            raise ModelRequestCancelledError(error.message)
        if error.error_code == "model_rate_limited":
            raise ModelRateLimitedError(
                error.message,
                retry_after_seconds=error.retry_after_seconds,
            )
        if error.error_code == "structured_output_invalid":
            raise StructuredOutputInvalidError(
                error.message,
                prompt_tokens=0,
                completion_tokens=0,
            )
        raise ExecutorUnavailableError(error.message)
