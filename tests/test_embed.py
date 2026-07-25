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
