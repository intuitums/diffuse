"""Small LiteLLM embedding adapter with explicit model and dimension checks."""

from __future__ import annotations

import os

import litellm
from litellm.exceptions import AuthenticationError, PermissionDeniedError

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
DEFAULT_BATCH_SIZE = 100


class EmbeddingCredentialError(ValueError):
    """The embedding provider rejected the configured credential.

    Subclasses ValueError because that is the contract ``service.worker``
    classifies as non-retryable. ``indexer`` deliberately does not import from
    ``service``, so this cannot reuse ``NonRetryableError`` directly.
    """


def embedding_model() -> str:
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def embedding_dimensions() -> int:
    value = int(os.environ.get("EMBEDDING_DIMENSIONS", DEFAULT_EMBEDDING_DIMENSIONS))
    if value <= 0:
        raise ValueError("EMBEDDING_DIMENSIONS must be positive")
    return value


def embedding_batch_size() -> int:
    value = int(os.environ.get("EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    if value <= 0:
        raise ValueError("EMBEDDING_BATCH_SIZE must be positive")
    return value


def _embedding_from_item(item: object) -> list[float]:
    value = item["embedding"] if isinstance(item, dict) else item.embedding
    return list(value)


def embedding_api_base() -> str | None:
    """An operator-run OpenAI-compatible embedding endpoint, or None.

    This is the embedding counterpart of ``REVIEW_API_BASE``, and it exists so
    that running Diffuse does not require an OpenAI account. Any server speaking
    the OpenAI embeddings API works -- Ollama, vLLM, Text Embeddings Inference,
    a LiteLLM proxy, or Azure OpenAI through a compatible gateway.

    LiteLLM would also honour an ambient ``OPENAI_BASE_URL``/``OPENAI_API_BASE``,
    and that does redirect embeddings today. It is the wrong lever: those names
    are read for *every* OpenAI-family call, so an operator redirecting
    embeddings to a local server would silently redirect an OpenAI review model
    to the same server, sending a managed provider's model name and credential
    to a host that is not theirs. That is the misrouting ``REVIEW_API_BASE``
    already guards against for review. This variable is passed per call, so it
    moves embeddings and nothing else.

    ``service.worker`` validates the URL at startup through the same
    ``_probe_base_url`` used for the GitHub origins, which is where the scheme
    and credential checks live; ``indexer`` cannot import them without depending
    on ``service``.
    """
    return os.environ.get("EMBEDDING_API_BASE", "").strip().rstrip("/") or None


def _resolves_own_credential(model: str) -> bool:
    """Whether Diffuse resolves this model's key rather than LiteLLM's ambient chain."""
    return model.startswith(("openai/", "text-embedding-"))


# The OpenAI client refuses to issue a request without a credential -- "Missing
# credentials. Please pass an `api_key` ..." -- even when `api_base` points at a
# server on the operator's own network that ignores the header entirely. Sending
# a constant to that server is what an operator would otherwise be told to do by
# hand, so Diffuse does it instead of failing on a credential nobody needs. This
# is not a secret and never reaches a managed provider: it is used only when the
# operator configured their own endpoint and set no key.
_SELF_HOSTED_PLACEHOLDER_KEY = "diffuse-self-hosted-endpoint"


def _provider_api_key(model: str) -> str | None:
    if not _resolves_own_credential(model):
        return None
    configured = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    if configured:
        return configured
    return _SELF_HOSTED_PLACEHOLDER_KEY if embedding_api_base() else None


def verify_embedding_credential() -> None:
    """Fail fast when the configured embedding model has no usable credential.

    Only models whose key Diffuse resolves itself can be checked. Every other
    provider authenticates from the ambient environment inside LiteLLM, which
    this cannot see, so an unset key there is not evidence of misconfiguration.

    Without this the failure surfaces at job-execution time as an
    ``InternalServerError`` -- LiteLLM maps a missing key onto the same class it
    uses for provider outages -- which ``run_once`` correctly treats as
    retryable. A permanently missing key then re-clones and re-parses the
    repository once per attempt, backing off across five attempts before it
    dead-letters, and reports nothing an operator can act on.
    """
    model = embedding_model()
    if not _resolves_own_credential(model) or _provider_api_key(model):
        return
    if embedding_api_base():
        # An operator-run endpoint decides its own authentication, and most
        # self-hosted embedding servers accept an unauthenticated request from
        # inside the deployment's own network. Demanding an OPENAI_API_KEY here
        # would force an operator with no OpenAI account to invent a placeholder
        # to get past a check that is about OpenAI's credential, not theirs.
        return
    raise ValueError(
        f"EMBEDDING_MODEL is {model!r}, which authenticates with "
        "OPENAI_API_KEY, but neither OPENAI_API_KEY nor OPENAI_KEY is set. "
        "Indexing a repository requires embeddings, and a repository with "
        "no index cannot be reviewed. Set OPENAI_API_KEY, point "
        "EMBEDDING_API_BASE at an OpenAI-compatible endpoint you run, or point "
        "EMBEDDING_MODEL at a provider that authenticates from the ambient "
        "environment."
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    expected_dimensions = embedding_dimensions()
    output: list[list[float]] = []
    for start in range(0, len(texts), embedding_batch_size()):
        batch = texts[start : start + embedding_batch_size()]
        model = embedding_model()
        try:
            response = litellm.embedding(
                model=model,
                input=batch,
                api_key=_provider_api_key(model),
                api_base=embedding_api_base(),
            )
        except (AuthenticationError, PermissionDeniedError) as error:
            # A rejected or revoked key is permanent: the mirror is re-cloned and
            # the tree re-parsed on every attempt, and five attempts recover
            # nothing. ValueError is what the worker classifies as non-retryable,
            # so this dead-letters immediately with the provider's own reason.
            raise EmbeddingCredentialError(
                f"Embedding provider rejected the credential for {model!r}: {error}"
            ) from error
        embeddings = [_embedding_from_item(item) for item in response.data]
        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"Embedding provider returned {len(embeddings)} vectors for {len(batch)} inputs"
            )
        for embedding in embeddings:
            if len(embedding) != expected_dimensions:
                # Provider/model drift is transient from Diffuse's point of view:
                # retrying may recover after a rollout or routing glitch, while a
                # ValueError would permanently fail the index job (DEV-199).
                raise RuntimeError(
                    f"Embedding model returned {len(embedding)} dimensions; "
                    f"expected {expected_dimensions}"
                )
        output.extend(embeddings)
    return output


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
