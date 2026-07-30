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
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)

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


def _captured_embedding(calls: list[dict]):
    def record(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])

    return record


def test_self_hosted_endpoint_removes_the_openai_account_requirement(monkeypatch):
    """An operator running their own embedding server should need no OpenAI key.

    `verify_embedding_credential` refuses to start without OPENAI_API_KEY because
    a missing key otherwise dead-letters every index job. That reasoning is about
    OpenAI's credential; it does not apply to a server on the operator's own
    network. Anthropic publishes no embeddings API at all, so before this an
    operator who connected only Anthropic could not index anything.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://embeddings.internal/v1")

    embed.verify_embedding_credential()


def test_endpoint_is_passed_per_call_rather_than_set_ambiently(monkeypatch):
    """EMBEDDING_API_BASE must move embeddings and nothing else.

    LiteLLM would honour an ambient OPENAI_BASE_URL, but that applies to every
    OpenAI-family call, so it would also redirect an OpenAI REVIEW_MODEL to the
    operator's endpoint -- sending a managed provider's model name and
    credential to a host that is not theirs.
    """
    calls: list[dict] = []
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://embeddings.internal/v1/")
    monkeypatch.setattr(embed.litellm, "embedding", _captured_embedding(calls))

    embed.embed_texts(["one"])

    assert calls[0]["api_base"] == "https://embeddings.internal/v1"
    # LiteLLM's OpenAI handler refuses to send a request with no credential at
    # all, so a non-secret constant stands in for the key the endpoint does not
    # want.
    assert calls[0]["api_key"] == embed._SELF_HOSTED_PLACEHOLDER_KEY


def test_a_real_key_still_wins_over_the_placeholder(monkeypatch):
    """The placeholder is a fallback, not an override.

    Pointing EMBEDDING_API_BASE at an authenticating gateway is legitimate, and
    silently replacing a configured credential would make it fail closed.
    """
    calls: list[dict] = []
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://gateway.internal/v1")
    monkeypatch.setattr(embed.litellm, "embedding", _captured_embedding(calls))

    embed.embed_texts(["one"])

    assert calls[0]["api_key"] == "sk-real"


def test_no_endpoint_means_no_placeholder(monkeypatch):
    """Without an operator endpoint the missing-key guard must still fire.

    Otherwise the placeholder would be sent to api.openai.com, turning a startup
    failure that names the problem into a 401 on every index job.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)

    assert embed._provider_api_key("text-embedding-3-small") is None
    with pytest.raises(ValueError, match="EMBEDDING_API_BASE"):
        embed.verify_embedding_credential()


def test_unset_endpoint_is_not_sent_as_an_empty_string(monkeypatch):
    """litellm must receive None, not "", or it routes to an empty host."""
    calls: list[dict] = []
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setenv("EMBEDDING_API_BASE", "   ")
    monkeypatch.setattr(embed.litellm, "embedding", _captured_embedding(calls))

    embed.embed_texts(["one"])

    assert calls[0]["api_base"] is None


def test_ambient_provider_still_gets_no_placeholder_key(monkeypatch):
    """A managed non-OpenAI model must keep authenticating through LiteLLM.

    The placeholder exists for an endpoint the operator runs. Handing it to a
    provider whose key LiteLLM resolves from the ambient environment would
    replace a working credential with a string that is not one.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://embeddings.internal/v1")

    assert embed._provider_api_key("cohere/embed-english-v3.0") is None
