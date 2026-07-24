"""Small LiteLLM embedding adapter with explicit model and dimension checks."""

from __future__ import annotations

import os

import litellm

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
DEFAULT_BATCH_SIZE = 100


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


def _provider_api_key(model: str) -> str | None:
    if model.startswith(("openai/", "text-embedding-")):
        return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    return None


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    expected_dimensions = embedding_dimensions()
    output: list[list[float]] = []
    for start in range(0, len(texts), embedding_batch_size()):
        batch = texts[start : start + embedding_batch_size()]
        model = embedding_model()
        response = litellm.embedding(
            model=model,
            input=batch,
            api_key=_provider_api_key(model),
        )
        embeddings = [_embedding_from_item(item) for item in response.data]
        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"Embedding provider returned {len(embeddings)} vectors for {len(batch)} inputs"
            )
        for embedding in embeddings:
            if len(embedding) != expected_dimensions:
                raise ValueError(
                    f"Embedding model returned {len(embedding)} dimensions; "
                    f"expected {expected_dimensions}"
                )
        output.extend(embeddings)
    return output


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
