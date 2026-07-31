"""Postgres persistence for repositories, immutable indexes, and the code graph."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

from repository_policy.models import EMPTY_POLICY_FINGERPRINT, validate_repo_path

from .chunker import Chunk
from .graph import CodeRelationship, CodeSymbol
from .index_version import INDEX_FORMAT_VERSION

DEFAULT_DATABASE_URL = "postgresql://diffuse:diffuse-dev@localhost:5432/diffuse"
DEFAULT_SCM_BASE_URL = "https://github.com"
GRAPH_RELATIONSHIP_KINDS = ("calls", "imports", "inherits", "implements")
LEXICAL_TERM_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,127}$")


@dataclass(frozen=True)
class SnapshotHandle:
    repository_id: int
    snapshot_id: int
    previous_snapshot_id: int | None
    state: str

    @property
    def should_build(self) -> bool:
        return self.state == "created"


def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))


def _path_prefix_filter(
    column: str,
    path_prefix: str | None,
) -> tuple[str | None, list[str]]:
    if path_prefix is None:
        return None, []
    normalized = validate_repo_path(path_prefix)
    escaped = normalized.replace("%", r"\%").replace("_", r"\_")
    return (
        f"({column} = %s OR {column} LIKE %s ESCAPE '\\')",
        [normalized, f"{escaped}/%"],
    )


def begin_index_snapshot(
    conn,
    repo: str,
    commit_sha: str,
    *,
    scm_provider: str = "github",
    scm_base_url: str = DEFAULT_SCM_BASE_URL,
    default_branch: str | None = None,
    stale_after_seconds: int = 3600,
    index_format_version: str = INDEX_FORMAT_VERSION,
) -> SnapshotHandle:
    """Create one building snapshot while serializing requests for the repository."""
    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    if not index_format_version.strip():
        raise ValueError("index_format_version cannot be empty")
    normalized_base_url = scm_base_url.rstrip("/")
    lock_identity = f"{scm_provider}\0{normalized_base_url}\0{repo}".encode()
    lock_id = int.from_bytes(
        hashlib.sha256(lock_identity).digest()[:8],
        byteorder="big",
        signed=True,
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        cursor.execute(
            """
            INSERT INTO repositories (
                scm_provider,
                scm_base_url,
                full_name,
                default_branch
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (scm_provider, scm_base_url, full_name)
            DO UPDATE SET
                default_branch = COALESCE(
                    EXCLUDED.default_branch,
                    repositories.default_branch
                ),
                updated_at = now()
            RETURNING id
            """,
            (scm_provider, normalized_base_url, repo, default_branch),
        )
        repository_id = int(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT id
            FROM index_snapshots
            WHERE repository_id = %s
              AND status = 'active'
            """,
            (repository_id,),
        )
        active_row = cursor.fetchone()
        previous_snapshot_id = int(active_row[0]) if active_row else None

        cursor.execute(
            """
            SELECT id
            FROM index_snapshots
            WHERE repository_id = %s
              AND commit_sha = %s
              AND index_format_version = %s
              AND status = 'active'
            """,
            (repository_id, commit_sha, index_format_version),
        )
        active_match = cursor.fetchone()
        if active_match:
            return SnapshotHandle(
                repository_id=repository_id,
                snapshot_id=int(active_match[0]),
                previous_snapshot_id=int(active_match[0]),
                state="active",
            )

        cursor.execute(
            """
            UPDATE index_snapshots
            SET status = 'failed',
                failure_code = 'stale_build',
                updated_at = now()
            WHERE repository_id = %s
              AND commit_sha = %s
              AND index_format_version = %s
              AND status = 'building'
              AND updated_at < now() - (%s * interval '1 second')
            """,
            (
                repository_id,
                commit_sha,
                index_format_version,
                stale_after_seconds,
            ),
        )
        cursor.execute(
            """
            SELECT id
            FROM index_snapshots
            WHERE repository_id = %s
              AND commit_sha = %s
              AND index_format_version = %s
              AND status = 'building'
            ORDER BY id DESC
            LIMIT 1
            """,
            (repository_id, commit_sha, index_format_version),
        )
        building_match = cursor.fetchone()
        if building_match:
            return SnapshotHandle(
                repository_id=repository_id,
                snapshot_id=int(building_match[0]),
                previous_snapshot_id=previous_snapshot_id,
                state="building",
            )

        cursor.execute(
            """
            INSERT INTO index_snapshots (
                repository_id,
                commit_sha,
                status,
                index_format_version,
                policy_fingerprint
            )
            VALUES (%s, %s, 'building', %s, %s)
            RETURNING id
            """,
            (
                repository_id,
                commit_sha,
                index_format_version,
                EMPTY_POLICY_FINGERPRINT,
            ),
        )
        snapshot_id = int(cursor.fetchone()[0])
        return SnapshotHandle(
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            previous_snapshot_id=previous_snapshot_id,
            state="created",
        )


def touch_snapshot(conn, snapshot_id: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE index_snapshots
            SET updated_at = now()
            WHERE id = %s
              AND status = 'building'
            """,
            (snapshot_id,),
        )


def mark_snapshot_failed(conn, snapshot_id: int, failure_code: str = "indexing_failed") -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE index_snapshots
            SET status = 'failed',
                failure_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status = 'building'
            """,
            (failure_code, snapshot_id),
        )


def activate_snapshot(conn, snapshot_id: int) -> bool:
    """Activate only if no newer viable snapshot request exists for the repository."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT repository_id
            FROM index_snapshots
            WHERE id = %s
              AND status = 'building'
            """,
            (snapshot_id,),
        )
        snapshot = cursor.fetchone()
        if not snapshot:
            return False
        repository_id = int(snapshot[0])

        cursor.execute(
            "SELECT id FROM repositories WHERE id = %s FOR UPDATE",
            (repository_id,),
        )
        cursor.fetchone()
        cursor.execute(
            """
            SELECT max(id)
            FROM index_snapshots
            WHERE repository_id = %s
              AND status IN ('building', 'active')
            """,
            (repository_id,),
        )
        newest_viable_id = int(cursor.fetchone()[0])
        if newest_viable_id != snapshot_id:
            cursor.execute(
                """
                UPDATE index_snapshots
                SET status = 'superseded',
                    updated_at = now()
                WHERE id = %s
                  AND status = 'building'
                """,
                (snapshot_id,),
            )
            return False

        cursor.execute(
            """
            UPDATE index_snapshots
            SET status = 'superseded',
                updated_at = now()
            WHERE repository_id = %s
              AND status = 'active'
            """,
            (repository_id,),
        )
        cursor.execute(
            """
            UPDATE index_snapshots
            SET status = 'active',
                activated_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status = 'building'
            """,
            (snapshot_id,),
        )
        return cursor.rowcount == 1


def _active_snapshot_id(
    conn,
    repo: str,
    *,
    scm_provider: str = "github",
    scm_base_url: str = DEFAULT_SCM_BASE_URL,
    index_format_version: str = INDEX_FORMAT_VERSION,
) -> int | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot.id
            FROM repositories AS repository
            JOIN index_snapshots AS snapshot
              ON snapshot.repository_id = repository.id
             AND snapshot.status = 'active'
            WHERE repository.scm_provider = %s
              AND repository.scm_base_url = %s
              AND repository.full_name = %s
              AND repository.enabled = TRUE
              AND snapshot.index_format_version = %s
            """,
            (
                scm_provider,
                scm_base_url.rstrip("/"),
                repo,
                index_format_version,
            ),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


def active_snapshot_id(
    conn,
    repo: str,
    *,
    scm_provider: str = "github",
    scm_base_url: str = DEFAULT_SCM_BASE_URL,
    index_format_version: str = INDEX_FORMAT_VERSION,
) -> int | None:
    return _active_snapshot_id(
        conn,
        repo,
        scm_provider=scm_provider,
        scm_base_url=scm_base_url,
        index_format_version=index_format_version,
    )


def active_snapshot_id_for_repository(
    conn,
    repository_id: int,
    *,
    index_format_version: str = INDEX_FORMAT_VERSION,
) -> int | None:
    """Resolve an active compatible snapshot by durable repository identity."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot.id
            FROM repositories AS repository
            JOIN index_snapshots AS snapshot
              ON snapshot.repository_id = repository.id
             AND snapshot.status = 'active'
            WHERE repository.id = %s
              AND repository.enabled = TRUE
              AND snapshot.index_format_version = %s
            """,
            (
                repository_id,
                index_format_version,
            ),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


def get_existing_hashes(
    conn,
    snapshot_id: int | None,
) -> dict[tuple[str, int, int], str]:
    if snapshot_id is None:
        return {}
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT file_path, start_line, end_line, content_hash
            FROM code_chunks
            WHERE snapshot_id = %s
            """,
            (snapshot_id,),
        )
        return {(row[0], row[1], row[2]): row[3] for row in cursor.fetchall()}


def copy_unchanged_chunks(
    conn,
    source_snapshot_id: int | None,
    target_snapshot_id: int,
    keys: list[tuple[str, int, int]],
) -> None:
    if source_snapshot_id is None or not keys:
        return
    source_id = int(source_snapshot_id)
    target_id = int(target_snapshot_id)
    with conn.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            f"""
            WITH keys (file_path, start_line, end_line) AS (VALUES %s)
            INSERT INTO code_chunks (
                snapshot_id,
                file_path,
                symbol_name,
                start_line,
                end_line,
                content_hash,
                content
            )
            SELECT
                {target_id},
                source.file_path,
                source.symbol_name,
                source.start_line,
                source.end_line,
                source.content_hash,
                source.content
            FROM code_chunks AS source
            JOIN keys USING (file_path, start_line, end_line)
            WHERE source.snapshot_id = {source_id}
            ON CONFLICT (snapshot_id, file_path, start_line, end_line) DO NOTHING
            """,
            keys,
            template="(%s, %s, %s)",
        )


def upsert_chunks(
    conn,
    snapshot_id: int,
    chunks: list[Chunk],
) -> None:
    if not chunks:
        return

    rows = [
        (
            snapshot_id,
            chunk.file_path,
            chunk.symbol_name,
            chunk.start_line,
            chunk.end_line,
            chunk.content_hash,
            chunk.content,
        )
        for chunk in chunks
    ]
    with conn.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """
            INSERT INTO code_chunks (
                snapshot_id,
                file_path,
                symbol_name,
                start_line,
                end_line,
                content_hash,
                content
            )
            VALUES %s
            ON CONFLICT (snapshot_id, file_path, start_line, end_line)
            DO UPDATE SET
                symbol_name = EXCLUDED.symbol_name,
                content_hash = EXCLUDED.content_hash,
                content = EXCLUDED.content
            """,
            rows,
        )


def write_symbol_graph(
    conn,
    snapshot_id: int,
    symbols: list[CodeSymbol],
    relationships: list[CodeRelationship],
) -> None:
    """Write one snapshot's graph without mutating any previously active snapshot."""
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM code_symbols WHERE snapshot_id = %s", (snapshot_id,))

        if symbols:
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO code_symbols (
                    snapshot_id,
                    stable_key,
                    file_path,
                    language,
                    kind,
                    name,
                    qualified_name,
                    start_line,
                    end_line,
                    signature,
                    docstring,
                    content_hash
                )
                VALUES %s
                """,
                [
                    (
                        snapshot_id,
                        symbol.stable_key,
                        symbol.file_path,
                        symbol.language,
                        symbol.kind,
                        symbol.name,
                        symbol.qualified_name,
                        symbol.start_line,
                        symbol.end_line,
                        symbol.signature,
                        symbol.docstring,
                        symbol.content_hash,
                    )
                    for symbol in symbols
                ],
            )

        if relationships:
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO code_relationships (
                    snapshot_id,
                    source_symbol_key,
                    target_symbol_key,
                    target_qualified_name,
                    kind,
                    line
                )
                VALUES %s
                """,
                [
                    (
                        snapshot_id,
                        relationship.source_symbol_key,
                        relationship.target_symbol_key,
                        relationship.target_qualified_name,
                        relationship.kind,
                        relationship.line,
                    )
                    for relationship in relationships
                ],
            )


def validate_snapshot_ready(
    conn,
    snapshot_id: int,
    *,
    expected_chunks: int,
    expected_symbols: int,
    expected_relationships: int,
    expected_policy_layers: int = 0,
    expected_guidance_documents: int = 0,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM code_chunks WHERE snapshot_id = %s),
                (SELECT count(*) FROM code_symbols WHERE snapshot_id = %s),
                (SELECT count(*) FROM code_relationships WHERE snapshot_id = %s),
                (SELECT count(*) FROM repository_policy_layers WHERE snapshot_id = %s),
                (
                    SELECT count(*)
                    FROM repository_guidance_documents
                    WHERE snapshot_id = %s
                )
            """,
            (snapshot_id, snapshot_id, snapshot_id, snapshot_id, snapshot_id),
        )
        actual = tuple(int(value) for value in cursor.fetchone())
    expected = (
        expected_chunks,
        expected_symbols,
        expected_relationships,
        expected_policy_layers,
        expected_guidance_documents,
    )
    if actual != expected:
        raise RuntimeError(
            "Snapshot validation failed: "
            "expected chunks/symbols/relationships/policy layers/guidance documents="
            f"{expected}, got {actual}"
        )


def search_lexical(
    conn,
    repo: str,
    terms: list[str] | tuple[str, ...],
    *,
    top_k: int = 8,
    exclude_files: set[str] | None = None,
    path_prefix: str | None = None,
    snapshot_id: int | None = None,
) -> list[dict]:
    """Search code/path/symbol text inside one compatible immutable snapshot."""
    if not 1 <= top_k <= 50:
        raise ValueError("top_k must be between 1 and 50")
    normalized_terms = list(dict.fromkeys(term.casefold() for term in terms))
    if len(normalized_terms) > 50:
        raise ValueError("At most 50 lexical terms may be searched")
    if any(not LEXICAL_TERM_PATTERN.fullmatch(term) for term in normalized_terms):
        raise ValueError("Lexical terms must be bounded code identifiers")
    if not normalized_terms:
        return []
    if snapshot_id is None:
        snapshot_id = _active_snapshot_id(conn, repo)
    if snapshot_id is None:
        return []

    query_text = " OR ".join(f'"{term}"' for term in normalized_terms)
    clauses = ["chunk.snapshot_id = %s", "chunk.search_vector @@ query.value"]
    parameters: list[object] = [query_text, snapshot_id]
    if exclude_files:
        clauses.append("chunk.file_path <> ALL(%s)")
        parameters.append(sorted(exclude_files))
    path_clause, path_parameters = _path_prefix_filter(
        "chunk.file_path",
        path_prefix,
    )
    if path_clause:
        clauses.append(path_clause)
        parameters.extend(path_parameters)
    parameters.append(top_k)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH query AS (
                SELECT websearch_to_tsquery('simple'::regconfig, %s) AS value
            )
            SELECT
                chunk.file_path,
                chunk.symbol_name,
                chunk.start_line,
                chunk.end_line,
                chunk.content,
                ts_rank_cd(chunk.search_vector, query.value, 32) AS lexical_rank
            FROM code_chunks AS chunk
            CROSS JOIN query
            WHERE {" AND ".join(clauses)}
            ORDER BY
                lexical_rank DESC,
                chunk.file_path,
                chunk.start_line,
                chunk.end_line
            LIMIT %s
            """,
            parameters,
        )
        return list(cursor.fetchall())


def search_graph_related_chunks(
    conn,
    repo: str,
    changed_ranges: dict[str, list[tuple[int, int]]],
    *,
    limit: int = 8,
    path_prefix: str | None = None,
    snapshot_id: int | None = None,
) -> list[dict]:
    """Return chunks for one-hop callers, callees, imports, and base/derived symbols."""
    if not 1 <= limit <= 50 or not changed_ranges:
        return []
    if snapshot_id is None:
        snapshot_id = _active_snapshot_id(conn, repo)
    if snapshot_id is None:
        return []

    changed_keys: set[str] = set()
    with conn.cursor() as cursor:
        for file_path, ranges in changed_ranges.items():
            valid_ranges = [
                (max(1, int(start)), max(max(1, int(start)), int(end))) for start, end in ranges
            ]
            if not valid_ranges:
                continue
            range_sql = " OR ".join("(start_line <= %s AND end_line >= %s)" for _ in valid_ranges)
            parameters: list[object] = [snapshot_id, file_path]
            for start, end in valid_ranges:
                parameters.extend([end, start])
            cursor.execute(
                f"""
                SELECT stable_key
                FROM code_symbols
                WHERE snapshot_id = %s
                  AND file_path = %s
                  AND kind <> 'module'
                  AND ({range_sql})
                """,
                parameters,
            )
            changed_keys.update(row[0] for row in cursor.fetchall())

        if not changed_keys:
            return []

        ordered_changed_keys = sorted(changed_keys)
        cursor.execute(
            """
            SELECT source_symbol_key, target_symbol_key, kind
            FROM code_relationships
            WHERE snapshot_id = %s
              AND kind = ANY(%s)
              AND target_symbol_key IS NOT NULL
              AND (
                  source_symbol_key = ANY(%s)
                  OR target_symbol_key = ANY(%s)
              )
            ORDER BY id
            LIMIT 200
            """,
            (
                snapshot_id,
                list(GRAPH_RELATIONSHIP_KINDS),
                ordered_changed_keys,
                ordered_changed_keys,
            ),
        )
        relationships = cursor.fetchall()

    neighbor_reasons: dict[str, list[str]] = {}
    for source_key, target_key, kind in relationships:
        if source_key in changed_keys and target_key not in changed_keys:
            neighbor_reasons.setdefault(target_key, []).append(f"{kind}:outbound")
        if target_key in changed_keys and source_key not in changed_keys:
            neighbor_reasons.setdefault(source_key, []).append(f"{kind}:inbound")
    if not neighbor_reasons:
        return []

    neighbor_keys = list(neighbor_reasons)
    path_clause, path_parameters = _path_prefix_filter(
        "symbol.file_path",
        path_prefix,
    )
    result_clauses = [
        "symbol.snapshot_id = %s",
        "symbol.stable_key = ANY(%s)",
        "symbol.kind <> 'module'",
    ]
    if path_clause:
        result_clauses.append(path_clause)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT ON (symbol.stable_key)
                symbol.stable_key,
                symbol.name AS symbol_name,
                chunk.file_path,
                chunk.start_line,
                chunk.end_line,
                chunk.content
            FROM code_symbols AS symbol
            JOIN code_chunks AS chunk
              ON chunk.snapshot_id = symbol.snapshot_id
             AND chunk.file_path = symbol.file_path
             AND chunk.start_line <= symbol.end_line
             AND chunk.end_line >= symbol.start_line
            WHERE {" AND ".join(result_clauses)}
            ORDER BY
                symbol.stable_key,
                (chunk.end_line - chunk.start_line),
                chunk.start_line
            """,
            (snapshot_id, neighbor_keys, *path_parameters),
        )
        rows = list(cursor.fetchall())

    order = {stable_key: index for index, stable_key in enumerate(neighbor_keys)}
    rows.sort(key=lambda row: order.get(row["stable_key"], len(order)))
    output: list[dict] = []
    for row in rows[:limit]:
        stable_key = row.pop("stable_key")
        row["retrieval_reason"] = "graph:" + ",".join(sorted(set(neighbor_reasons[stable_key])))
        output.append(row)
    return output
