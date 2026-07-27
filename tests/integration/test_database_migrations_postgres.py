import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2 import sql

from service.database_migrations import (
    METADATA_TABLE,
    Migration,
    MigrationDriftError,
    UnversionedDatabaseError,
    load_migration_catalog,
    migrate_database,
    migration_status,
    verify_database_current,
)


def _database_url(base_url: str, database_name: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database_name}",
            parsed.query,
            parsed.fragment,
        )
    )


@pytest.fixture
def migration_database():
    admin_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    database_name = f"diffuse_migration_{uuid.uuid4().hex}"
    with closing(psycopg2.connect(admin_url)) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
    target_url = _database_url(admin_url, database_name)
    try:
        yield target_url
    finally:
        with closing(psycopg2.connect(admin_url)) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s
                      AND pid <> pg_backend_pid()
                    """,
                    (database_name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(database_name)
                    )
                )


def test_fresh_migration_is_repeatable_and_verifiable(migration_database):
    expected_versions = [
        migration.version for migration in load_migration_catalog()
    ]
    expected_latest = expected_versions[-1]
    with closing(psycopg2.connect(migration_database)) as connection:
        before = migration_status(connection)
        first = migrate_database(connection, actor="integration-test")
        after = verify_database_current(connection)
        with connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO api_tokens (
                    name,
                    token_sha256,
                    scopes,
                    all_repositories,
                    created_by
                )
                VALUES (
                    'api-generation-test',
                    %s,
                    ARRAY['diffuse:api:generate'],
                    TRUE,
                    'integration-test'
                )
                """,
                ("a" * 64,),
            )
            cursor.execute(
                "SELECT to_regclass('public.api_idempotency_keys')"
            )
            assert cursor.fetchone()[0] == "api_idempotency_keys"
        second = migrate_database(connection, actor="integration-test")

    assert before.state == "empty"
    assert before.current_version == 0
    assert first.before_version == 0
    assert first.after_version == expected_latest
    assert [
        migration.version for migration in first.applied
    ] == expected_versions
    assert not first.adopted_baseline
    assert after.current
    assert after.current_version == expected_latest
    assert second.before_version == second.after_version == expected_latest
    assert second.applied == ()


def test_concurrent_migrators_serialize_and_apply_each_version_once(
    migration_database,
):
    baseline = load_migration_catalog()[0]
    second_sql = (
        "CREATE TABLE migration_probe ("
        "id BIGINT PRIMARY KEY, value TEXT NOT NULL"
        ");"
    )
    second = Migration(
        version=2,
        name="migration_probe",
        checksum=hashlib.sha256(second_sql.encode()).hexdigest(),
        sql=second_sql,
        source_path="integration:0002_migration_probe.sql",
    )
    catalog = (baseline, second)

    def migrate():
        with closing(psycopg2.connect(migration_database)) as connection:
            return migrate_database(
                connection,
                actor="concurrency-test",
                migrations=catalog,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: migrate(), range(2)))

    assert sorted(len(result.applied) for result in results) == [0, 2]
    with closing(psycopg2.connect(migration_database)) as connection:
        status = verify_database_current(connection, migrations=catalog)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.migration_probe')")
            probe = cursor.fetchone()[0]
            cursor.execute(
                f"""
                SELECT version, count(*)
                FROM {METADATA_TABLE}
                GROUP BY version
                ORDER BY version
                """
            )
            counts = cursor.fetchall()
    assert status.current_version == 2
    assert probe == "migration_probe"
    assert counts == [(1, 1), (2, 1)]


def test_unversioned_schema_requires_explicit_verified_adoption(
    migration_database,
):
    catalog = load_migration_catalog()
    baseline = catalog[0]
    with closing(psycopg2.connect(migration_database)) as connection:
        with connection, connection.cursor() as cursor:
            cursor.execute(baseline.sql)
        assert migration_status(connection).state == "unversioned"
        with pytest.raises(UnversionedDatabaseError, match="--adopt-existing"):
            migrate_database(connection, actor="integration-test")
        adopted = migrate_database(
            connection,
            actor="operator@example.com",
            adopt_existing=True,
        )
        status = verify_database_current(connection)

    assert adopted.adopted_baseline
    assert len(adopted.applied) == len(catalog)
    assert [migration.adopted for migration in adopted.applied] == [
        True,
        *([False] * (len(catalog) - 1)),
    ]
    assert status.current
    assert status.applied[0].adopted


def test_applied_checksum_drift_fails_closed(migration_database):
    with closing(psycopg2.connect(migration_database)) as connection:
        migrate_database(connection, actor="integration-test")
        with connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {METADATA_TABLE} SET checksum = %s WHERE version = 1",
                ("0" * 64,),
            )
        with pytest.raises(MigrationDriftError, match="immutable"):
            migration_status(connection)
