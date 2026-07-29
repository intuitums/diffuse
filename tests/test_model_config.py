import hashlib
import json

import pytest

from service.model_config import (
    DEFAULT_REVIEW_MODEL,
    ModelExecutor,
    StructuredOutputMode,
    load_model_execution_config,
)


def test_default_execution_configuration_preserves_litellm(monkeypatch):
    for name in (
        "DIFFUSE_CONFIG_FILE",
        "REVIEW_EXECUTOR",
        "REVIEW_MODEL",
        "REVIEW_VERIFIER_MODEL",
        "REVIEW_STRUCTURED_OUTPUT_MODE",
        "REVIEW_API_BASE",
        "DIFFUSE_MODEL_RUNNER_SOCKET",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_model_execution_config()

    assert config.executor is ModelExecutor.LITELLM
    assert config.review_model == DEFAULT_REVIEW_MODEL
    assert config.verifier_model == DEFAULT_REVIEW_MODEL
    assert config.structured_output_mode is StructuredOutputMode.AUTO
    assert len(config.plan.fingerprint) == 64


def test_toml_configuration_has_environment_compatibility_overrides(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "diffuse.toml"
    path.write_text(
        """
[models]
executor = "codex-cli"
review_model = "gpt-5-codex"
verifier_model = "gpt-5-codex"
structured_output_mode = "schema"

[model_runner]
socket = "/var/run/diffuse-test.sock"
"""
    )
    monkeypatch.setenv("DIFFUSE_CONFIG_FILE", str(path))
    monkeypatch.setenv("REVIEW_MODEL", "operator-selected")

    config = load_model_execution_config()

    assert config.executor is ModelExecutor.CODEX_CLI
    assert config.review_model == "operator-selected"
    assert config.verifier_model == "gpt-5-codex"
    assert str(config.runner_socket) == "/var/run/diffuse-test.sock"


def test_cli_configuration_rejects_prompt_only_output_and_api_base(monkeypatch):
    monkeypatch.setenv("REVIEW_EXECUTOR", "codex-cli")
    monkeypatch.setenv("REVIEW_STRUCTURED_OUTPUT_MODE", "prompt")
    with pytest.raises(ValueError, match="native schema"):
        load_model_execution_config()

    monkeypatch.setenv("REVIEW_STRUCTURED_OUTPUT_MODE", "schema")
    monkeypatch.setenv("REVIEW_API_BASE", "https://models.example.test/v1")
    with pytest.raises(ValueError, match="litellm"):
        load_model_execution_config()


def test_execution_plan_fingerprint_includes_executor(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "same-model")
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", "same-model")
    monkeypatch.setenv("REVIEW_EXECUTOR", "litellm")
    provider = load_model_execution_config().plan

    monkeypatch.setenv("REVIEW_EXECUTOR", "codex-cli")
    cli = load_model_execution_config().plan

    assert provider.fingerprint != cli.fingerprint
    assert (
        provider.fingerprint
        == hashlib.sha256(
            json.dumps(
                provider.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
