import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from indexer.chunker import Chunk
from indexer.store import (
    activate_snapshot,
    begin_index_snapshot,
    upsert_chunks,
    validate_snapshot_ready,
)
from service import code_query


def _embedding(index: int) -> list[float]:
    output = [0.0] * 1536
    output[index] = 1.0
    return output


def test_authorized_code_search_stays_pinned_after_snapshot_supersession(
    monkeypatch,
):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repository_name = f"query/{uuid.uuid4().hex}"
    remote_url = "https://github.example.com"
    first_commit = "a" * 40
    second_commit = "b" * 40
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("EMBEDDING_MODEL", "integration-code-query")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setattr(
        "retriever.retrieve.embed_text",
        lambda _query: _embedding(0),
    )

    with closing(psycopg2.connect(database_url)) as connection:
        first = begin_index_snapshot(
            connection,
            repository_name,
            first_commit,
            "integration-code-query",
            1536,
            scm_base_url=remote_url,
            default_branch="main",
        )
        upsert_chunks(
            connection,
            first.snapshot_id,
            1536,
            [
                Chunk(
                    file_path="src/auth.py",
                    start_line=1,
                    end_line=3,
                    content=(
                        "def requireTenantAuthorization(account, tenant):\n"
                        "    return account.tenant_id == tenant.id"
                    ),
                    symbol_name="requireTenantAuthorization",
                ),
                Chunk(
                    file_path="docs/overview.md",
                    start_line=1,
                    end_line=1,
                    content="Welcome to the service.",
                    symbol_name=None,
                ),
            ],
            [_embedding(0), _embedding(1)],
        )
        validate_snapshot_ready(
            connection,
            first.snapshot_id,
            expected_chunks=2,
            expected_symbols=0,
            expected_relationships=0,
        )
        assert activate_snapshot(connection, first.snapshot_id)
        connection.commit()

        target = code_query.resolve_code_query_target(
            connection,
            repository_name=repository_name,
            remote="github",
            remote_url=remote_url,
            default_branch="main",
            authorized_repository_ids=frozenset({first.repository_id}),
        )

        with pytest.raises(ValueError, match="not authorized"):
            code_query.resolve_code_query_target(
                connection,
                repository_name=repository_name,
                remote="github",
                remote_url=remote_url,
                default_branch="main",
                authorized_repository_ids=frozenset(
                    {first.repository_id + 100_000}
                ),
            )

        second = begin_index_snapshot(
            connection,
            repository_name,
            second_commit,
            "integration-code-query",
            1536,
            scm_base_url=remote_url,
            default_branch="main",
        )
        upsert_chunks(
            connection,
            second.snapshot_id,
            1536,
            [
                Chunk(
                    file_path="src/replacement.py",
                    start_line=1,
                    end_line=2,
                    content="def replacement():\n    return None",
                    symbol_name="replacement",
                )
            ],
            [_embedding(1)],
        )
        validate_snapshot_ready(
            connection,
            second.snapshot_id,
            expected_chunks=1,
            expected_symbols=0,
            expected_relationships=0,
        )
        assert activate_snapshot(connection, second.snapshot_id)
        connection.commit()

    result = code_query.search_codebase(
        target,
        query="Where is requireTenantAuthorization implemented?",
        path_prefix="src",
    )
    scoped_out = code_query.search_codebase(
        target,
        query="Where is requireTenantAuthorization implemented?",
        path_prefix="docs",
    )

    assert result["resultCount"] == 1
    source = result["sources"][0]
    assert source["filePath"] == "src/auth.py"
    assert source["snapshotId"] == first.snapshot_id
    assert source["commitSha"] == first_commit
    assert first_commit in source["sourceUrl"]
    assert second_commit not in source["sourceUrl"]
    assert result["provenance"]["indexSnapshots"][0]["snapshotId"] == (
        first.snapshot_id
    )
    assert scoped_out["sources"] == []
