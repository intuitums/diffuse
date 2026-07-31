from unittest.mock import patch

import pytest

from indexer.store import search_lexical


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


def test_lexical_search_uses_bounded_websearch_query_and_snapshot():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_lexical(
            connection,
            "owner/repo",
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


def test_search_path_prefix_is_literal_and_follows_snapshot_parameters():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_lexical(
            connection,
            "owner/repo",
            ["retry_payment"],
            top_k=3,
            path_prefix="src/tenant_%/auth",
        )

    assert connection.cursor_instance.parameters == [
        '"retry_payment"',
        42,
        "src/tenant_%/auth",
        r"src/tenant\_\%/auth/%",
        3,
    ]
    assert "LIKE %s ESCAPE" in connection.cursor_instance.query


def test_search_without_an_active_compatible_snapshot_returns_no_rows():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=None):
        rows = search_lexical(connection, "owner/repo", ["retry_payment"])

    assert rows == []


def test_lexical_search_rejects_unbounded_or_syntax_bearing_terms():
    connection = _Connection()

    with pytest.raises(ValueError, match="code identifiers"):
        search_lexical(connection, "owner/repo", ["unsafe | query"])

    with pytest.raises(ValueError, match="At most 50"):
        search_lexical(
            connection,
            "owner/repo",
            [f"term_{index}" for index in range(51)],
        )
