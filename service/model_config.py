"""Typed, non-secret configuration for Diffuse model execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_REVIEW_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_RUNNER_SOCKET = "/run/diffuse/model-runner.sock"
EXECUTION_PLAN_SCHEMA_VERSION = "diffuse-execution-plan-v1"
RUNNER_PROTOCOL_VERSION = "diffuse-model-runner-v1"


class ModelExecutor(StrEnum):
    LITELLM = "litellm"
    CODEX_CLI = "codex-cli"
    CLAUDE_CLI = "claude-cli"


class StructuredOutputMode(StrEnum):
    AUTO = "auto"
    SCHEMA = "schema"
    PROMPT = "prompt"


@dataclass(frozen=True)
class ModelTarget:
    executor: ModelExecutor
    requested_model: str
    structured_output_mode: StructuredOutputMode

    def __post_init__(self) -> None:
        if not self.requested_model.strip() or len(self.requested_model) > 512:
            raise ValueError("Model target requires a model name of at most 512 characters")

    def to_dict(self) -> dict[str, str]:
        return {
            "executor": self.executor.value,
            "requested_model": self.requested_model,
            "structured_output_mode": self.structured_output_mode.value,
        }


@dataclass(frozen=True)
class ModelExecutionPlan:
    candidate: ModelTarget
    verifier: ModelTarget
    runner_protocol_version: str = RUNNER_PROTOCOL_VERSION
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.candidate.executor is not self.verifier.executor:
            raise ValueError("Mixed model executors are not supported")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", self.runner_protocol_version):
            raise ValueError("Runner protocol version is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runner_protocol_version": self.runner_protocol_version,
            "candidate": self.candidate.to_dict(),
            "verifier": self.verifier.to_dict(),
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ModelExecutionConfig:
    executor: ModelExecutor
    review_model: str
    verifier_model: str
    structured_output_mode: StructuredOutputMode
    runner_socket: Path

    @property
    def plan(self) -> ModelExecutionPlan:
        return self.plan_for_models(self.review_model, self.verifier_model)

    def plan_for_models(
        self,
        candidate_model: str,
        verifier_model: str,
    ) -> ModelExecutionPlan:
        return ModelExecutionPlan(
            candidate=ModelTarget(
                executor=self.executor,
                requested_model=candidate_model,
                structured_output_mode=self.structured_output_mode,
            ),
            verifier=ModelTarget(
                executor=self.executor,
                requested_model=verifier_model,
                structured_output_mode=self.structured_output_mode,
            ),
        )


def _configuration_file() -> dict[str, Any]:
    configured = os.environ.get("DIFFUSE_CONFIG_FILE", "").strip()
    if not configured:
        return {}
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("DIFFUSE_CONFIG_FILE must be an absolute path")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Cannot read DIFFUSE_CONFIG_FILE: {error}") from error
    if len(payload) > 1_048_576:
        raise ValueError("DIFFUSE_CONFIG_FILE must be at most 1 MiB")
    try:
        parsed = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("DIFFUSE_CONFIG_FILE is not valid UTF-8 TOML") from error
    if not isinstance(parsed, dict):
        raise ValueError("DIFFUSE_CONFIG_FILE must contain a TOML object")
    return parsed


def _table(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"DIFFUSE_CONFIG_FILE [{name}] must be a table")
    return value


def _setting(
    environment_name: str,
    table: dict[str, Any],
    file_name: str,
    default: str,
) -> str:
    environment_value = os.environ.get(environment_name)
    if environment_value is not None:
        return environment_value.strip()
    file_value = table.get(file_name, default)
    if not isinstance(file_value, str):
        raise ValueError(f"{file_name} must be a string")
    return file_value.strip()


def load_model_execution_config() -> ModelExecutionConfig:
    """Resolve file configuration and backward-compatible environment overrides.

    The TOML file contains routing and process locations only. Provider secrets
    remain environment/secret-manager inputs and are deliberately not parsed here.
    """

    config = _configuration_file()
    models = _table(config, "models")
    runner = _table(config, "model_runner")
    raw_executor = _setting("REVIEW_EXECUTOR", models, "executor", ModelExecutor.LITELLM)
    try:
        executor = ModelExecutor(raw_executor)
    except ValueError as error:
        raise ValueError("REVIEW_EXECUTOR must be litellm, codex-cli, or claude-cli") from error

    default_model = DEFAULT_REVIEW_MODEL if executor is ModelExecutor.LITELLM else "default"
    review_model = _setting("REVIEW_MODEL", models, "review_model", default_model)
    verifier_model = (
        _setting(
            "REVIEW_VERIFIER_MODEL",
            models,
            "verifier_model",
            "",
        )
        or review_model
    )
    raw_mode = _setting(
        "REVIEW_STRUCTURED_OUTPUT_MODE",
        models,
        "structured_output_mode",
        StructuredOutputMode.AUTO,
    )
    try:
        structured_output_mode = StructuredOutputMode(raw_mode)
    except ValueError as error:
        raise ValueError("REVIEW_STRUCTURED_OUTPUT_MODE must be auto, schema, or prompt") from error
    if (
        executor is not ModelExecutor.LITELLM
        and structured_output_mode is StructuredOutputMode.PROMPT
    ):
        raise ValueError("CLI executors require native schema-constrained output")
    if executor is not ModelExecutor.LITELLM and os.environ.get("REVIEW_API_BASE"):
        raise ValueError("REVIEW_API_BASE applies only when REVIEW_EXECUTOR=litellm")
    if not review_model:
        raise ValueError("REVIEW_MODEL cannot be empty")

    socket_value = _setting(
        "DIFFUSE_MODEL_RUNNER_SOCKET",
        runner,
        "socket",
        DEFAULT_RUNNER_SOCKET,
    )
    runner_socket = Path(socket_value)
    if not runner_socket.is_absolute():
        raise ValueError("DIFFUSE_MODEL_RUNNER_SOCKET must be an absolute path")
    return ModelExecutionConfig(
        executor=executor,
        review_model=review_model,
        verifier_model=verifier_model,
        structured_output_mode=structured_output_mode,
        runner_socket=runner_socket,
    )
