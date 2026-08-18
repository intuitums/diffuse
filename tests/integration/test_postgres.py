import hashlib
import os
from contextlib import closing

import psycopg2
import pytest
from diffuse.repository.cross_repository import (
    CrossRepositoryContextError,
    add_repository_cluster_member,
    create_repository_cluster,
    list_repository_clusters,
    remove_repository_cluster_member,
    resolve_cross_repository_context_plan,
)
from diffuse.repository.indexing.chunker import Chunk, chunk_repo
from diffuse.repository.indexing.file_index import IndexedFile
from diffuse.repository.indexing.graph import (
    CodeRelationship,
    CodeSymbol,
    extract_repository_graph,
)
from diffuse.repository.indexing.index_version import INDEX_FORMAT_VERSION
from diffuse.repository.indexing.store import (
    activate_snapshot,
    active_snapshot_id,
    begin_index_snapshot,
    copy_unchanged_chunks,
    copy_unchanged_files,
    get_existing_file_hashes,
    get_existing_hashes,
    search_graph_related_chunks,
    search_grep,
    search_lexical,
    upsert_chunks,
    upsert_repository_files,
    validate_snapshot_ready,
    write_symbol_graph,
)
from diffuse.repository.policy.models import (
    GuidanceDocument,
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
)
from diffuse.repository.policy.resolve import resolve_review_policy
from diffuse.repository.policy.store import load_repository_policy, write_repository_policy
from diffuse.repository.registry import register_repository


def _symbol(key: str, name: str, start_line: int, end_line: int) -> CodeSymbol:
    return CodeSymbol(
        stable_key=key,
        file_path="app.py",
        language="python",
        kind="function",
        name=name,
        qualified_name=f"app.{name}",
        start_line=start_line,
        end_line=end_line,
        signature=f"def {name}()",
        docstring=None,
        content_hash=f"{key}-hash",
    )


def _chunk(name: str, start_line: int, end_line: int) -> Chunk:
    return Chunk(
        file_path="app.py",
        start_line=start_line,
        end_line=end_line,
        content=f"def {name}():\n    return True",
        symbol_name=name,
    )


def _file(content: str) -> IndexedFile:
    return IndexedFile(file_path="app.py", content=content)


def test_snapshot_activation_reuse_and_graph_lexical_retrieval():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    caller = _symbol("integration-caller-key", "caller", 1, 2)
    callee = _symbol("integration-callee-key", "callee", 4, 5)
    relationship = CodeRelationship(
        source_symbol_key=caller.stable_key,
        target_symbol_key=callee.stable_key,
        target_qualified_name=callee.qualified_name,
        kind="calls",
        line=2,
    )
    chunks = [_chunk("caller", 1, 2), _chunk("callee", 4, 5)]
    files = [_file("def caller():\n    return callee()\n\ndef callee():\n    return True\n")]

    with closing(psycopg2.connect(database_url)) as connection:
        first = begin_index_snapshot(
            connection,
            "integration/repo",
            "a" * 40,
        )
        upsert_chunks(connection, first.snapshot_id, chunks)
        upsert_repository_files(connection, first.snapshot_id, files)
        write_symbol_graph(
            connection,
            first.snapshot_id,
            [caller, callee],
            [relationship],
        )
        validate_snapshot_ready(
            connection,
            first.snapshot_id,
            expected_chunks=2,
            expected_symbols=2,
            expected_relationships=1,
            expected_files=1,
        )
        assert activate_snapshot(connection, first.snapshot_id)
        assert (
            active_snapshot_id(connection, "integration/repo")
            == first.snapshot_id
        )
        assert (
            active_snapshot_id(
                connection,
                "integration/repo",
                index_format_version="incompatible-index-format",
            )
            is None
        )

        graph_rows = search_graph_related_chunks(
            connection,
            "integration/repo",
            {"app.py": [(1, 2)]},
        )
        assert len(graph_rows) == 1
        assert graph_rows[0]["symbol_name"] == "callee"
        assert graph_rows[0]["retrieval_reason"] == "graph:calls:outbound"
        lexical_rows = search_lexical(
            connection,
            "integration/repo",
            ["callee"],
        )
        assert len(lexical_rows) == 1
        assert lexical_rows[0]["symbol_name"] == "callee"
        assert float(lexical_rows[0]["lexical_rank"]) > 0
        grep_rows = search_grep(connection, "integration/repo", "return callee()")
        assert [(row["file_path"], row["start_line"], row["content"]) for row in grep_rows] == [
            ("app.py", 2, "    return callee()")
        ]
        assert (
            search_lexical(
                connection,
                "integration/repo",
                ["callee"],
                exclude_files={"app.py"},
            )
            == []
        )

        second = begin_index_snapshot(
            connection,
            "integration/repo",
            "b" * 40,
        )
        hashes = get_existing_hashes(connection, second.previous_snapshot_id)
        file_hashes = get_existing_file_hashes(connection, second.previous_snapshot_id)
        assert len(hashes) == 2
        assert len(file_hashes) == 1
        copy_unchanged_chunks(
            connection,
            second.previous_snapshot_id,
            second.snapshot_id,
            list(hashes),
        )
        copy_unchanged_files(
            connection,
            second.previous_snapshot_id,
            second.snapshot_id,
            list(file_hashes),
        )
        write_symbol_graph(
            connection,
            second.snapshot_id,
            [caller, callee],
            [relationship],
        )
        validate_snapshot_ready(
            connection,
            second.snapshot_id,
            expected_chunks=2,
            expected_symbols=2,
            expected_relationships=1,
            expected_files=1,
        )
        assert activate_snapshot(connection, second.snapshot_id)

        copied_rows = search_lexical(
            connection,
            "integration/repo",
            ["caller"],
            top_k=1,
        )
        third = begin_index_snapshot(
            connection,
            "integration/repo",
            "c" * 40,
        )
        duplicate_third = begin_index_snapshot(
            connection,
            "integration/repo",
            "c" * 40,
        )
        assert duplicate_third.state == "building"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT index_format_version FROM index_snapshots WHERE id = %s",
                (first.snapshot_id,),
            )
            stored_index_format_version = cursor.fetchone()[0]
            cursor.execute(
                """
                UPDATE index_snapshots
                SET updated_at = now() - interval '2 hours'
                WHERE id = %s
                """,
                (third.snapshot_id,),
            )
        recovered_third = begin_index_snapshot(
            connection,
            "integration/repo",
            "c" * 40,
            stale_after_seconds=1,
        )
        assert recovered_third.state == "created"
        assert recovered_third.snapshot_id != third.snapshot_id

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status
                FROM index_snapshots
                WHERE repository_id = %s
                ORDER BY id
                """,
                (second.repository_id,),
            )
            statuses = cursor.fetchall()
        connection.rollback()

    assert statuses[:2] == [
        (first.snapshot_id, "superseded"),
        (second.snapshot_id, "active"),
    ]
    assert statuses[2:] == [
        (third.snapshot_id, "failed"),
        (recovered_third.snapshot_id, "building"),
    ]
    assert len(copied_rows) == 1
    assert copied_rows[0]["symbol_name"] == "caller"
    assert stored_index_format_version == INDEX_FORMAT_VERSION


def test_multilanguage_graph_persists_and_expands_to_imported_callee(tmp_path):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "lib.js").write_text(
        "export function normalize(value) { return value.trim(); }\n"
    )
    (source_dir / "service.js").write_text(
        'import { normalize as clean } from "./lib.js";\n'
        "export function save(value) { return clean(value); }\n"
    )
    (tmp_path / "worker.go").write_text(
        "package worker\n"
        "type Job struct { ID string }\n"
        "func (job *Job) Run() string { return job.ID }\n"
    )
    graph = extract_repository_graph(tmp_path)
    chunks = chunk_repo(tmp_path, symbols=graph.symbols)

    with closing(psycopg2.connect(database_url)) as connection:
        snapshot = begin_index_snapshot(
            connection,
            "integration/multilanguage",
            "d" * 40,
        )
        upsert_chunks(connection, snapshot.snapshot_id, chunks)
        write_symbol_graph(
            connection,
            snapshot.snapshot_id,
            list(graph.symbols),
            list(graph.relationships),
        )
        validate_snapshot_ready(
            connection,
            snapshot.snapshot_id,
            expected_chunks=len(chunks),
            expected_symbols=len(graph.symbols),
            expected_relationships=len(graph.relationships),
        )
        assert activate_snapshot(connection, snapshot.snapshot_id)

        related = search_graph_related_chunks(
            connection,
            "integration/multilanguage",
            {"src/service.js": [(2, 2)]},
        )
        lexical = search_lexical(
            connection,
            "integration/multilanguage",
            ["normalize"],
            exclude_files={"src/service.js"},
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT language, count(*)
                FROM code_symbols
                WHERE snapshot_id = %s
                GROUP BY language
                ORDER BY language
                """,
                (snapshot.snapshot_id,),
            )
            language_counts = dict(cursor.fetchall())
        connection.rollback()

    assert any(
        row["symbol_name"] == "normalize"
        and row["retrieval_reason"] == "graph:calls:outbound"
        for row in related
    )
    assert any(
        row["file_path"] == "src/lib.js"
        and row["symbol_name"] == "normalize"
        and float(row["lexical_rank"]) > 0
        for row in lexical
    )
    assert language_counts["go"] >= 3
    assert language_counts["javascript"] >= 4


def test_repository_policy_is_immutable_snapshot_data():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    content = "Every database query must include the tenant identifier.\n"
    policy = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "review": {
                            "passes": ["security"],
                            "minimum_confidence": 0.9,
                        },
                    }
                ),
            ),
        ),
        guidance_documents=(
            GuidanceDocument(
                directory_path="",
                source_path="AGENTS.md",
                kind="instructions",
                applies_to=("src/**",),
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                priority=10,
            ),
        ),
        skipped_sources=("CLAUDE.md",),
    )

    with closing(psycopg2.connect(database_url)) as connection:
        snapshot = begin_index_snapshot(
            connection,
            "integration/policy",
            "e" * 40,
        )
        write_repository_policy(connection, snapshot.snapshot_id, policy)
        validate_snapshot_ready(
            connection,
            snapshot.snapshot_id,
            expected_chunks=0,
            expected_symbols=0,
            expected_relationships=0,
            expected_policy_layers=1,
            expected_guidance_documents=1,
        )
        assert activate_snapshot(connection, snapshot.snapshot_id)
        loaded = load_repository_policy(connection, snapshot.snapshot_id)
        resolved = resolve_review_policy(loaded, {"src/api.py", "README.md"})
        with pytest.raises(ValueError, match="building snapshot"):
            write_repository_policy(connection, snapshot.snapshot_id, policy)
        connection.rollback()

    assert loaded == policy
    assert resolved.for_path("src/api.py").minimum_confidence == 0.9
    assert len(resolved.for_path("src/api.py").guidance_documents) == 1
    assert resolved.for_path("README.md").guidance_documents == ()


def test_repository_clusters_resolve_exact_bounded_same_host_context_snapshots():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]

    with closing(psycopg2.connect(database_url)) as connection:
        repositories = {
            name: register_repository(
                connection,
                scm_provider="github",
                scm_base_url="https://github.com",
                full_name=f"integration-context/{name}",
                default_branch="main",
            )
            for name in ("app", "shared", "sdk", "unindexed")
        }
        external = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.example.com",
            full_name="integration-context/external",
            default_branch="main",
        )

        snapshots = {}
        for ordinal, name in enumerate(("app", "shared", "sdk"), start=1):
            snapshot = begin_index_snapshot(
                connection,
                f"integration-context/{name}",
                str(ordinal) * 40,
            )
            assert activate_snapshot(connection, snapshot.snapshot_id)
            snapshots[name] = snapshot

        cluster = create_repository_cluster(
            connection,
            name="integration-stack",
            repository_ids=tuple(
                repository.id
                for repository in repositories.values()
            ),
            actor_login="integration-operator",
        )
        with pytest.raises(ValueError, match="same SCM provider and host"):
            create_repository_cluster(
                connection,
                name="invalid-cross-host",
                repository_ids=(repositories["app"].id, external.id),
                actor_login="integration-operator",
            )

        plan = resolve_cross_repository_context_plan(
            connection,
            primary_repository_id=repositories["app"].id,
            primary_snapshot_id=snapshots["app"].snapshot_id,
            explicit_repositories=("integration-context/shared",),
        )
        repeated = resolve_cross_repository_context_plan(
            connection,
            primary_repository_id=repositories["app"].id,
            primary_snapshot_id=snapshots["app"].snapshot_id,
            explicit_repositories=("integration-context/shared",),
        )

        assert plan.primary_commit_sha == "1" * 40
        assert [
            (
                item.repository_full_name,
                item.snapshot_id,
                item.commit_sha,
                item.source,
                item.cluster_ids,
            )
            for item in plan.related_snapshots
        ] == [
            (
                "integration-context/shared",
                snapshots["shared"].snapshot_id,
                "2" * 40,
                "explicit+cluster",
                (cluster.id,),
            ),
            (
                "integration-context/sdk",
                snapshots["sdk"].snapshot_id,
                "3" * 40,
                "cluster",
                (cluster.id,),
            ),
        ]
        assert repeated.fingerprint == plan.fingerprint
        assert list_repository_clusters(connection)[-1] == cluster

        assert remove_repository_cluster_member(
            connection,
            cluster_id=cluster.id,
            repository_id=repositories["unindexed"].id,
        )
        assert add_repository_cluster_member(
            connection,
            cluster_id=cluster.id,
            repository_id=repositories["unindexed"].id,
            actor_login="integration-operator",
        )
        with pytest.raises(CrossRepositoryContextError, match="same SCM host"):
            resolve_cross_repository_context_plan(
                connection,
                primary_repository_id=repositories["app"].id,
                primary_snapshot_id=snapshots["app"].snapshot_id,
                explicit_repositories=("integration-context/missing",),
            )
        connection.rollback()
