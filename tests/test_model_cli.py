from service.model_cli import model_status


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
