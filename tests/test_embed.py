from types import SimpleNamespace

import pytest

from indexer import embed


def test_embedding_dimension_mismatch_raises_runtime_error(monkeypatch):
    """Wrong vector width must not be a ValueError — those fail jobs permanently."""

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3")
    monkeypatch.setattr(
        embed.litellm,
        "embedding",
        lambda **_kwargs: SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2])]
        ),
    )

    with pytest.raises(RuntimeError, match="dimensions") as raised:
        embed.embed_texts(["one"])

    assert not isinstance(raised.value, ValueError)


def test_embedding_count_mismatch_also_raises_runtime_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setattr(
        embed.litellm,
        "embedding",
        lambda **_kwargs: SimpleNamespace(data=[]),
    )

    with pytest.raises(RuntimeError, match="vectors"):
        embed.embed_texts(["one"])


def test_missing_openai_key_fails_before_any_job_runs(monkeypatch):
    """A missing key must stop startup, not dead-letter through five attempts.

    LiteLLM maps an absent credential onto InternalServerError, the same class it
    uses for a provider outage, so the worker correctly treats it as retryable.
    Each attempt re-clones the mirror and re-parses the tree before failing
    again, and the operator sees only a traceback.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        embed.verify_embedding_credential()


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "OPENAI_KEY"])
def test_either_openai_variable_satisfies_the_check(monkeypatch, variable):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.setenv(variable, "sk-test")

    embed.verify_embedding_credential()


def test_ambient_credential_providers_are_not_second_guessed(monkeypatch):
    """Only models whose key Diffuse resolves itself can be checked here.

    Every other provider authenticates inside LiteLLM from the ambient
    environment, which this cannot see, so an unset OPENAI_API_KEY there is not
    evidence of anything and must not block startup.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "bedrock/amazon.titan-embed-text-v2:0")

    embed.verify_embedding_credential()


def test_rejected_credential_is_non_retryable(monkeypatch):
    """A revoked key is permanent; retrying re-clones the repository for nothing."""

    def reject(**_kwargs):
        raise embed.AuthenticationError(
            message="invalid api key", llm_provider="openai", model="text-embedding-3-small"
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-revoked")
    monkeypatch.setattr(embed.litellm, "embedding", reject)

    with pytest.raises(embed.EmbeddingCredentialError) as raised:
        embed.embed_texts(["one"])

    # ValueError is the contract service.worker classifies as terminal.
    assert isinstance(raised.value, ValueError)
