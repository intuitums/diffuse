"""Every model-identifier call site must agree on one prefix table."""

import pytest

from service import model_cli
from service.model_providers import model_family, resolve_provider
from service.review_engine import _model_api_base, _model_api_key

CREDENTIAL_VARIABLES = (
    "OPENAI_API_KEY",
    "OPENAI_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_PROFILE",
    "REVIEW_API_BASE",
)

# model identifier, provider id, family, credential required, takes REVIEW_API_BASE
RESOLUTION_CASES = (
    ("openai/gpt-4.1-mini", "openai", "openai", True, True),
    ("gpt-4.1-mini", "openai", "openai", True, True),
    ("o3-mini", "openai", "openai", True, True),
    ("anthropic/claude-sonnet-4-6", "anthropic", "anthropic", True, False),
    ("claude-sonnet-5", "anthropic", "anthropic", True, False),
    ("Claude-Sonnet-5", "anthropic", "anthropic", True, False),
    ("openrouter/anthropic/claude-sonnet-4.6", "openrouter", "anthropic", True, False),
    ("openrouter/openai/gpt-5.2", "openrouter", "openai", True, False),
    ("gemini/gemini-2.5-pro", "google", "google", True, False),
    ("gemini-3-pro", "google", "google", True, False),
    ("google/gemini-3-pro", "google", "google", True, False),
    ("vertex_ai/gemini-2.5-pro", "google", "google", True, False),
    ("azure/gpt-4o", "azure", "openai", True, False),
    ("azure_ai/gpt-5", "azure", "openai", True, False),
    ("bedrock/anthropic.claude-v2", "aws-bedrock", "anthropic", True, False),
    ("ollama/qwen3-coder", "self-hosted", None, False, True),
    ("hosted_vllm/qwen3-coder", "self-hosted", None, False, True),
    ("some-unprefixed-id", "custom", None, False, True),
)


@pytest.fixture(autouse=True)
def _clear_credentials(monkeypatch):
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("model", "provider_id", "family", "requires_credential", "takes_api_base"),
    RESOLUTION_CASES,
)
def test_provider_resolution_is_consistent_across_call_sites(
    monkeypatch,
    model: str,
    provider_id: str,
    family: str | None,
    requires_credential: bool,
    takes_api_base: bool,
) -> None:
    record = resolve_provider(model)

    assert record.provider_id == provider_id
    assert record.credential_required is requires_credential
    assert model_family(model) == family

    # model_cli must report the same provider the engine authenticates against.
    monkeypatch.setenv("REVIEW_MODEL", model)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", model)
    assert model_cli.model_status()["provider"] == provider_id

    monkeypatch.setenv("REVIEW_API_BASE", "https://vllm.internal/v1")
    resolved_base = _model_api_base(model)
    assert (resolved_base is not None) is takes_api_base


@pytest.mark.parametrize(
    ("model", "provider_id", "family", "requires_credential", "takes_api_base"),
    RESOLUTION_CASES,
)
def test_a_required_credential_is_never_reported_as_configured_when_absent(
    monkeypatch,
    model: str,
    provider_id: str,
    family: str | None,
    requires_credential: bool,
    takes_api_base: bool,
) -> None:
    """`diffuse model` must not claim readiness for an unset credential.

    Regression: unrecognised prefixes such as `vertex_ai/` and `azure_ai/` fell
    through to a `custom` branch declaring no credential requirement, so the
    readiness command reported a deployment ready with nothing configured.
    """

    monkeypatch.setenv("REVIEW_MODEL", model)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", model)

    status = model_cli.model_status()

    assert status["credential_configured"] is not requires_credential
    if requires_credential:
        assert _model_api_key(model) is None


def test_gemini_accepts_either_google_or_gemini_key(monkeypatch) -> None:
    """The readiness report must not contradict the key the engine will send."""

    monkeypatch.setenv("REVIEW_MODEL", "gemini/gemini-2.5-pro")
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", "gemini/gemini-2.5-pro")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")

    status = model_cli.model_status()

    assert status["credential_configured"] is True
    assert _model_api_key("gemini/gemini-2.5-pro") == "google-secret"
    assert "google-secret" not in repr(status)


def test_bedrock_requires_a_profile_or_a_complete_key_pair(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MODEL", "bedrock/anthropic.claude-v2")
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", "bedrock/anthropic.claude-v2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "only-half")

    assert model_cli.model_status()["credential_configured"] is False

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other-half")
    assert model_cli.model_status()["credential_configured"] is True
