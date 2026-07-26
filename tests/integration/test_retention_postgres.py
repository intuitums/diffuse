import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from service.retention_store import (
    MAX_SNAPSHOTS_PER_PURGE,
    RETENTION_PURGES,
    purge_audit_events,
    purge_code_chunks,
    purge_expired_records,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
        reason="POSTGRES_TEST_DATABASE_URL is not configured",
    ),
]


def _insert_audit_event(connection, *, resource_id: str, age_days: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit_events (
                actor_kind,
                actor_label,
                action,
                resource_kind,
                resource_id,
                occurred_at
            )
            VALUES (
                'system',
                'retention-test',
                'retention.test',
                'retention_test',
                %s,
                now() - make_interval(days => %s)
            )
            """,
            (resource_id, age_days),
        )


def _audit_resource_ids(connection, *, resource_id_prefix: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT resource_id
            FROM audit_events
            WHERE resource_kind = 'retention_test'
              AND resource_id LIKE %s
            """,
            (f"{resource_id_prefix}%",),
        )
        return {row[0] for row in cursor.fetchall()}


def test_every_retention_statement_runs_against_the_real_schema():
    # The retention SQL is the only place Diffuse deletes by time, and every
    # statement uses locking clauses the planner accepts only in specific
    # shapes. Executing each one here is what proves they are valid.
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]

    with closing(psycopg2.connect(database_url)) as connection, connection:
        removed = purge_expired_records(connection)

    assert set(removed) == {table for table, _purge in RETENTION_PURGES}
    assert all(count >= 0 for count in removed.values())


def test_audit_events_past_their_window_are_purged_and_recent_ones_survive(
    monkeypatch,
):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    prefix = uuid.uuid4().hex[:12]
    monkeypatch.setenv("DIFFUSE_AUDIT_EVENT_RETENTION_DAYS", "7")

    with closing(psycopg2.connect(database_url)) as connection:
        try:
            with connection:
                _insert_audit_event(
                    connection,
                    resource_id=f"{prefix}-expired",
                    age_days=30,
                )
                _insert_audit_event(
                    connection,
                    resource_id=f"{prefix}-recent",
                    age_days=1,
                )

            with connection:
                purged = purge_audit_events(connection)

            with connection:
                surviving = _audit_resource_ids(
                    connection,
                    resource_id_prefix=prefix,
                )
        finally:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM audit_events
                    WHERE resource_kind = 'retention_test'
                      AND resource_id LIKE %s
                    """,
                    (f"{prefix}%",),
                )

    assert purged >= 1
    assert surviving == {f"{prefix}-recent"}


def test_code_chunks_keep_draining_after_the_first_batch_of_snapshots(monkeypatch):
    # `code_chunks` is the largest table in the schema and the only one holding
    # embeddings, and nothing ever deletes the `index_snapshots` rows that own
    # its chunks. A purge that reselects the same oldest retired snapshots on
    # every pass empties them once and then deletes nothing for the life of the
    # deployment, leaving the vectors of every later snapshot in place forever.
    # Everything here runs inside one transaction that is rolled back.
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    monkeypatch.setenv("DIFFUSE_CODE_CHUNK_RETENTION_DAYS", "30")
    snapshots = MAX_SNAPSHOTS_PER_PURGE + 3

    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO repositories (scm_provider, scm_base_url, full_name)
                VALUES ('github', 'https://retention.invalid', %s)
                RETURNING id
                """,
                (f"retention/{uuid.uuid4().hex[:12]}",),
            )
            repository_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO index_snapshots (
                    repository_id, commit_sha, status, index_format_version,
                    policy_fingerprint, embedding_model, embedding_dimensions,
                    updated_at
                )
                SELECT %s, 'abcdef1234567', 'superseded', 'v1', repeat('a', 64),
                       'retention-test', 1536, now() - make_interval(days => 90)
                FROM generate_series(1, %s)
                """,
                (repository_id, snapshots),
            )
            cursor.execute(
                """
                INSERT INTO code_chunks (
                    snapshot_id, file_path, start_line, end_line,
                    content_hash, content, embedding
                )
                SELECT id, 'retention.py', 1, 2, 'hash', 'body',
                       (
                           '['
                           || array_to_string(
                               array_fill(0.1::real, ARRAY[1536]), ','
                           )
                           || ']'
                       )::vector
                FROM index_snapshots
                WHERE repository_id = %s
                """,
                (repository_id,),
            )

        removed = []
        for _pass in range(5):
            deleted = purge_code_chunks(connection)
            removed.append(deleted)
            if deleted == 0:
                break

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM code_chunks
                JOIN index_snapshots ON index_snapshots.id = code_chunks.snapshot_id
                WHERE index_snapshots.repository_id = %s
                """,
                (repository_id,),
            )
            surviving = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM index_snapshots WHERE repository_id = %s",
                (repository_id,),
            )
            surviving_snapshots = cursor.fetchone()[0]
    finally:
        connection.rollback()
        connection.close()

    assert sum(removed) == snapshots
    # More than one pass: the first is capped at MAX_SNAPSHOTS_PER_PURGE
    # snapshots, so the rest only drain if the window advanced.
    assert len([deleted for deleted in removed if deleted]) > 1
    assert surviving == 0
    # Provenance is untouched; only the vectors are reclaimed.
    assert surviving_snapshots == snapshots
