from unittest.mock import patch

import pytest

from indexer.file_index import index_repository_files
from indexer.store import search_grep
from retriever.context_models import CrossRepositoryContextPlan
from service.code_query import CodeQueryTarget, _grep_literal, search_codebase


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


def test_file_index_preserves_safe_text_files_without_chunk_boundaries(tmp_path):
    (tmp_path / "app.py").write_text("needle = 'literal value'\n")
    (tmp_path / "empty.txt").write_text("")
    (tmp_path / "binary.dat").write_bytes(b"not\0text")
    (tmp_path / ".env").write_text("API_KEY=secret\n")

    files = index_repository_files(tmp_path)

    assert [(file.file_path, file.content) for file in files] == [
        ("app.py", "needle = 'literal value'\n"),
        ("empty.txt", ""),
    ]


def test_grep_search_uses_the_snapshot_scoped_trigram_candidate_filter():
    connection = _Connection()

    with patch("indexer.store._active_snapshot_id", return_value=42):
        search_grep(connection, "owner/repo", "needle", top_k=3)

    assert connection.cursor_instance.parameters == [42, "needle", "needle", 3]
    assert "FROM repository_files AS file" in connection.cursor_instance.query
    assert "file.content LIKE '%%' || %s || '%%'" in connection.cursor_instance.query
    assert "WITH ORDINALITY" in connection.cursor_instance.query


def test_grep_search_rejects_unselective_or_multiline_literals():
    connection = _Connection()

    with pytest.raises(ValueError, match="3 to 512"):
        search_grep(connection, "owner/repo", "if")
    with pytest.raises(ValueError, match="non-newline"):
        search_grep(connection, "owner/repo", "first\nsecond")


def test_grep_search_escapes_like_metacharacters_before_querying():
    connection = _Connection()

    search_grep(connection, "owner/repo", "sum_total%", snapshot_id=42)

    assert connection.cursor_instance.parameters == [42, r"sum\_total\%", r"sum\_total\%", 8]
    assert "ESCAPE '\\'" in connection.cursor_instance.query


def test_grep_mode_is_explicit_and_preserves_the_literal():
    assert _grep_literal("grep: retry_payment(") == "retry_payment("
    assert _grep_literal("Why does retry_payment fail?") is None
    with pytest.raises(ValueError, match="required"):
        _grep_literal("grep:   ")


def test_code_search_routes_only_the_explicit_prefix_to_grep(monkeypatch):
    target = CodeQueryTarget(
        repository_id=1,
        repository_name="owner/repo",
        remote="github",
        remote_url="https://github.com",
        default_branch="main",
        include_related=False,
        context_plan=CrossRepositoryContextPlan(
            primary_repository_id=1,
            primary_repository_full_name="owner/repo",
            primary_snapshot_id=2,
            primary_commit_sha="a" * 40,
        ),
    )
    calls = []
    monkeypatch.setattr(
        "service.code_query._retrieve_grep",
        lambda *_args, **kwargs: calls.append(kwargs) or ("needle", (), "f" * 64),
    )

    result = search_codebase(target, query="grep: needle", limit=3)

    assert calls == [{"query": "grep: needle", "path_prefix": None, "limit": 3}]
    assert result["query"] == "needle"
    assert result["searchMode"] == "grep"
