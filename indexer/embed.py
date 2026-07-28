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


def _resolves_own_credential(model: str) -> bool:
    """Whether Diffuse resolves this model's key rather than LiteLLM's ambient chain."""
    return model.startswith(("openai/", "text-embedding-"))


def _provider_api_key(model: str) -> str | None:
    if _resolves_own_credential(model):
        return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    return None


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
    if _resolves_own_credential(model) and not _provider_api_key(model):
        raise ValueError(
            f"EMBEDDING_MODEL is {model!r}, which authenticates with "
            "OPENAI_API_KEY, but neither OPENAI_API_KEY nor OPENAI_KEY is set. "
            "Indexing a repository requires embeddings, and a repository with "
            "no index cannot be reviewed. Set OPENAI_API_KEY, or point "
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
