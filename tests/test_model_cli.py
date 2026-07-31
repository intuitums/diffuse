import argparse
import json

from service.cli import model as model_cli
from service.cli.model import model_status


def test_openai_model_status_reports_names_without_secret(
    monkeypatch,
) -> None:
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")

    status = model_status()

    assert status["provider"] == "openai"
    assert status["credential_configured"] is True
    assert status["credential_env_names"] == ("OPENAI_API_KEY", "OPENAI_KEY")
    assert "secret-value" not in repr(status)


def test_anthropic_model_reports_missing_key(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MODEL", "anthropic/claude-sonnet-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    status = model_status()

    assert status["provider"] == "anthropic"
    assert status["credential_configured"] is False


def test_openrouter_cross_family_models_share_gateway_credential(monkeypatch) -> None:
    monkeypatch.setenv(
        "REVIEW_MODEL",
        "openrouter/anthropic/claude-sonnet-4.6",
    )
    monkeypatch.setenv(
        "REVIEW_VERIFIER_MODEL",
        "openrouter/openai/gpt-5.2",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")

    status = model_status()

    assert status["provider"] == "openrouter"
    assert status["verifier_provider"] == "openrouter"
    assert status["credential_env_names"] == ("OPENROUTER_API_KEY",)
    assert status["verifier_credential_configured"] is True
    assert status["cross_family_review"] is True
    assert "secret-value" not in repr(status)


def test_vertex_live_check_uses_application_default_credentials(
    monkeypatch,
    capsys,
) -> None:
    model = "vertex_ai/gemini-2.5-pro"
    monkeypatch.setenv("REVIEW_MODEL", model)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", model)
    monkeypatch.setenv("VERTEXAI_PROJECT", "diffuse-project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "us-central1")
    calls: list[str] = []
    monkeypatch.setattr(model_cli, "verify_model_connection", calls.append)

    model_cli._run(argparse.Namespace(live=True))

    status = json.loads(capsys.readouterr().out)
    assert calls == [model]
    assert status["credential_configured"] is True
    assert status["live_verified"] is True


def test_vertex_live_check_attempts_ambient_adc_without_static_hints(
    monkeypatch,
    capsys,
) -> None:
    model = "vertex_ai/gemini-2.5-pro"
    monkeypatch.setenv("REVIEW_MODEL", model)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", model)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("VERTEXAI_PROJECT", raising=False)
    monkeypatch.delenv("VERTEXAI_LOCATION", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(model_cli, "verify_model_connection", calls.append)

    model_cli._run(argparse.Namespace(live=True))

    status = json.loads(capsys.readouterr().out)
    assert calls == [model]
    assert status["credential_configured"] is False
    assert status["live_verified"] is True
