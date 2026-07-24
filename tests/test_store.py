from unittest.mock import patch

import pytest

from indexer.store import _vector_literal, search_lexical, search_similar


def test_vector_literal_validates_shape_and_values():
    assert _vector_literal([1, 2.5], 2) == "[1.0,2.5]"

    with pytest.raises(ValueError, match="dimensions"):
        _vector_literal([1], 2)

    with pytest.raises(ValueError, match="non-finite"):
        _vector_literal([float("nan")], 1)


class _Cursor:
    def __init__(self):
        self.query = ""
        self.parameters = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, parameters):
        self.query = query
        self.parameters = parameters

    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def cursor(self, **_kwargs):
        return self.cursor_instance


def test_search_parameters_follow_sql_placeholder_order():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_similar(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            [0.1, 0.2],
            top_k=3,
            exclude_files={"changed.py"},
        )

    assert connection.cursor_instance.parameters == [
        "[0.1,0.2]",
        42,
        ["changed.py"],
        "[0.1,0.2]",
        3,
    ]


def test_search_path_prefix_is_literal_and_follows_snapshot_parameters():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_similar(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            [0.1, 0.2],
            top_k=3,
            path_prefix="src/tenant_%/auth",
        )

    assert connection.cursor_instance.parameters == [
        "[0.1,0.2]",
        42,
        "src/tenant_%/auth",
        r"src/tenant\_\%/auth/%",
        "[0.1,0.2]",
        3,
    ]
    assert "LIKE %s ESCAPE" in connection.cursor_instance.query


def test_search_without_an_active_compatible_snapshot_returns_no_rows():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=None):
        rows = search_similar(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            [0.1, 0.2],
        )

    assert rows == []


def test_lexical_search_uses_bounded_websearch_query_and_snapshot():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_lexical(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            ["PaymentDeclinedError", "retry_payment", "paymentdeclinederror"],
            top_k=3,
            exclude_files={"changed.py"},
        )

    assert connection.cursor_instance.parameters == [
        '"paymentdeclinederror" OR "retry_payment"',
        42,
        ["changed.py"],
        3,
    ]
    assert "websearch_to_tsquery" in connection.cursor_instance.query
    assert "search_vector @@ query.value" in connection.cursor_instance.query


def test_lexical_search_rejects_unbounded_or_syntax_bearing_terms():
    connection = _Connection()

    with pytest.raises(ValueError, match="code identifiers"):
        search_lexical(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            ["unsafe | query"],
        )

    with pytest.raises(ValueError, match="At most 50"):
        search_lexical(
            connection,
            "owner/repo",
            "embedding-model",
            2,
            [f"term_{index}" for index in range(51)],
        )
