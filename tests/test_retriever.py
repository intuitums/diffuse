from unittest.mock import MagicMock, patch

import pytest

from retriever.context_models import (
    CrossRepositoryContextPlan,
    RepositoryContextSnapshot,
)
from retriever.retrieve import (
    RetrievedContext,
    build_retrieval_query,
    extract_lexical_terms,
    extract_query_terms,
    format_as_extra_instructions,
    normalize_code_query,
    parse_changed_files,
    parse_changed_line_ranges,
    retrieve_context,
    retrieve_context_from_plan,
    retrieve_context_with_snapshot,
    retrieve_query_context_from_plan,
)


def test_changed_files_and_query_ignore_diff_metadata():
    diff = """\
diff --git a/service/api.py b/service/api.py
--- a/service/api.py
+++ b/service/api.py
@@ -1 +1 @@
-return old_value
+return new_value
"""

    assert parse_changed_files(diff) == {"service/api.py"}
    query = build_retrieval_query(diff)
    assert "diff --git" in query
    assert "-return old_value" in query
    assert "+return new_value" in query
    assert "+++ b/service/api.py" not in query
    assert parse_changed_line_ranges(diff) == {"service/api.py": [(1, 1), (1, 1)]}


def test_changed_ranges_cover_renames_additions_and_deletions():
    diff = """\
diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -10,3 +20,0 @@
-removed
@@ -30,0 +40,2 @@
+added
"""

    assert parse_changed_line_ranges(diff) == {
        "old.py": [(10, 12), (30, 30)],
        "new.py": [(20, 20), (40, 41)],
    }


def test_lexical_terms_prioritize_specific_changed_identifiers():
    diff = """\
diff --git a/payments/service.py b/payments/service.py
--- a/payments/service.py
+++ b/payments/service.py
@@ -3 +3,2 @@
-raise LegacyPaymentError(error_code)
+raise PaymentDeclinedError(PAYMENT_DECLINED_CODE)
+return retry_payment(payment_id)
"""

    terms = extract_lexical_terms(diff)

    assert "PaymentDeclinedError" in terms
    assert "PAYMENT_DECLINED_CODE" in terms
    assert "retry_payment" in terms
    assert "LegacyPaymentError" in terms
    assert "return" not in terms
    assert len(terms) <= 24


def test_code_query_terms_are_bounded_and_ignore_question_filler():
    terms = extract_query_terms(
        "How does requireTenantAuthorization connect to account_repository?"
    )

    assert "requireTenantAuthorization" in terms
    assert "account_repository" in terms
    assert "How" not in terms
    assert "does" not in terms
    assert len(terms) <= 24

    with pytest.raises(ValueError, match="1 to 2000"):
        normalize_code_query("\x00")


def test_format_marks_context_untrusted_and_obeys_budget():
    context = RetrievedContext(
        file_path="service/example.py",
        symbol_name="example",
        start_line=1,
        end_line=200,
        content="x" * 2000,
        similarity=0.8123,
        retrieval_reason="semantic",
    )

    formatted = format_as_extra_instructions([context], max_chars=500)

    assert len(formatted) <= 500
    assert "untrusted reference code" in formatted
    assert "service/example.py:1-200" in formatted
    assert "truncated" in formatted


def test_format_uses_graph_provenance_when_similarity_is_not_applicable():
    context = RetrievedContext(
        file_path="service/caller.py",
        symbol_name="caller",
        start_line=10,
        end_line=20,
        content="def caller(): ...",
        similarity=None,
        retrieval_reason="graph:calls:inbound",
    )

    formatted = format_as_extra_instructions([context])

    assert "[graph:calls:inbound;" in formatted
    assert "hybrid_score=" in formatted


def test_retrieval_prioritizes_graph_context_then_fills_with_semantic_context():
    connection = MagicMock()
    graph_rows = [
        {
            "file_path": "service/caller.py",
            "symbol_name": "caller",
            "start_line": 10,
            "end_line": 20,
            "content": "def caller(): ...",
            "retrieval_reason": "graph:calls:inbound",
        }
    ]
    semantic_rows = [
        {
            "file_path": "service/pattern.py",
            "symbol_name": "pattern",
            "start_line": 1,
            "end_line": 8,
            "content": "def pattern(): ...",
            "similarity": 0.9,
        }
    ]
    lexical_rows = [
        {
            "file_path": "service/pattern.py",
            "symbol_name": "pattern",
            "start_line": 1,
            "end_line": 8,
            "content": "def pattern(): ...",
            "lexical_rank": 0.75,
        }
    ]
    diff = """\
--- a/service/api.py
+++ b/service/api.py
@@ -3 +3 @@
-old()
+new()
"""

    with (
        patch("retriever.retrieve.get_conn", side_effect=[connection, connection]),
        patch("retriever.retrieve.active_snapshot_id", return_value=17),
        patch("retriever.retrieve.embed_text", return_value=[0.1, 0.2]),
        patch(
            "retriever.retrieve.search_graph_related_chunks",
            return_value=graph_rows,
        ),
        patch("retriever.retrieve.search_lexical", return_value=lexical_rows),
        patch("retriever.retrieve.search_similar", return_value=semantic_rows),
    ):
        contexts = retrieve_context("owner/repo", diff, top_k=2)

    assert [context.retrieval_reason for context in contexts] == [
        "graph:calls:inbound",
        "lexical+semantic",
    ]
    assert contexts[0].relevance_score > contexts[1].relevance_score
    assert contexts[1].similarity == 0.9
    assert connection.close.call_count == 2


def test_retrieval_skips_embedding_when_no_active_snapshot():
    connection = MagicMock()
    diff = """\
--- a/service/api.py
+++ b/service/api.py
@@ -3 +3 @@
-old()
+new()
"""

    with (
        patch("retriever.retrieve.get_conn", return_value=connection),
        patch("retriever.retrieve.active_snapshot_id", return_value=None),
        patch("retriever.retrieve.embed_text") as embed,
    ):
        bundle = retrieve_context_with_snapshot("owner/repo", diff)

    assert bundle.snapshot_id is None
    assert bundle.contexts == ()
    embed.assert_not_called()


def test_cross_repository_retrieval_preserves_repository_provenance_and_path_identity():
    connection = MagicMock()
    plan = CrossRepositoryContextPlan(
        primary_repository_id=1,
        primary_repository_full_name="owner/app",
        primary_snapshot_id=11,
        primary_commit_sha="a" * 40,
        related_snapshots=(
            RepositoryContextSnapshot(
                repository_id=2,
                repository_full_name="owner/sdk",
                snapshot_id=22,
                commit_sha="b" * 40,
                source="explicit",
            ),
        ),
    )
    common = {
        "file_path": "src/client.py",
        "symbol_name": "Client",
        "start_line": 1,
        "end_line": 10,
    }

    def lexical(*_args, **kwargs):
        if kwargs["snapshot_id"] == 11:
            return [{**common, "content": "class AppClient: ...", "lexical_rank": 0.9}]
        return [{**common, "content": "class SDKClient: ...", "lexical_rank": 0.8}]

    def semantic(*_args, **kwargs):
        if kwargs["snapshot_id"] == 11:
            return [{**common, "content": "class AppClient: ...", "similarity": 0.8}]
        return [{**common, "content": "class SDKClient: ...", "similarity": 0.95}]

    diff = """\
--- a/src/api.py
+++ b/src/api.py
@@ -1 +1 @@
-LegacyClient()
+SDKClient()
"""
    with (
        patch("retriever.retrieve.get_conn", return_value=connection),
        patch("retriever.retrieve.embed_text", return_value=[0.1, 0.2]) as embed,
        patch("retriever.retrieve.search_graph_related_chunks", return_value=[]),
        patch("retriever.retrieve.search_lexical", side_effect=lexical) as lexical_search,
        patch("retriever.retrieve.search_similar", side_effect=semantic),
    ):
        bundle = retrieve_context_from_plan(diff, plan, top_k=2)

    assert embed.call_count == 1
    assert bundle.context_plan == plan
    assert {context.repository_full_name for context in bundle.contexts} == {
        "owner/app",
        "owner/sdk",
    }
    sdk_context = next(
        context
        for context in bundle.contexts
        if context.repository_full_name == "owner/sdk"
    )
    assert sdk_context.retrieval_reason == "cross-repo:lexical+cross-repo:semantic"
    assert "owner/sdk::src/client.py" in format_as_extra_instructions([sdk_context])
    assert lexical_search.call_args_list[0].kwargs["exclude_files"] == {"src/api.py"}
    assert "exclude_files" not in lexical_search.call_args_list[1].kwargs


def test_query_retrieval_uses_path_scoped_lexical_semantic_and_graph_channels():
    connection = MagicMock()
    plan = CrossRepositoryContextPlan(
        primary_repository_id=1,
        primary_repository_full_name="owner/app",
        primary_snapshot_id=11,
        primary_commit_sha="a" * 40,
    )
    lexical_rows = [
        {
            "file_path": "src/auth.py",
            "symbol_name": "authorize",
            "start_line": 10,
            "end_line": 20,
            "content": "def authorize(): ...",
            "lexical_rank": 0.9,
        }
    ]
    semantic_rows = [
        {
            **lexical_rows[0],
            "similarity": 0.92,
        },
        {
            "file_path": "src/noise.py",
            "symbol_name": "noise",
            "start_line": 1,
            "end_line": 4,
            "content": "def noise(): ...",
            "similarity": 0.1,
        },
    ]
    graph_rows = [
        {
            "file_path": "src/caller.py",
            "symbol_name": "handler",
            "start_line": 30,
            "end_line": 38,
            "content": "def handler(): authorize()",
            "retrieval_reason": "graph:calls:inbound",
        }
    ]

    with (
        patch("retriever.retrieve.get_conn", return_value=connection),
        patch("retriever.retrieve.embed_text", return_value=[0.1, 0.2]),
        patch(
            "retriever.retrieve.search_lexical",
            return_value=lexical_rows,
        ) as lexical,
        patch(
            "retriever.retrieve.search_similar",
            return_value=semantic_rows,
        ) as semantic,
        patch(
            "retriever.retrieve.search_graph_related_chunks",
            return_value=graph_rows,
        ) as graph,
    ):
        bundle = retrieve_query_context_from_plan(
            "Where is authorize called?",
            plan,
            top_k=2,
            path_prefix="src",
        )

    assert {context.file_path for context in bundle.contexts} == {
        "src/auth.py",
        "src/caller.py",
    }
    assert next(
        context
        for context in bundle.contexts
        if context.file_path == "src/auth.py"
    ).retrieval_reason == "lexical+semantic"
    assert lexical.call_args.kwargs["path_prefix"] == "src"
    assert semantic.call_args.kwargs["path_prefix"] == "src"
    assert graph.call_args.kwargs["path_prefix"] == "src"
    assert graph.call_args.args[4] == {"src/auth.py": [(10, 20)]}
