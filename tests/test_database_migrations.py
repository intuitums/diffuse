import hashlib
from pathlib import Path

import pytest

from service.database_migrations import (
    BASELINE_SCHEMA_SHA256,
    MigrationDriftError,
    MigrationError,
    _baseline_contract,
    load_migration_catalog,
)
from service.review_cli import _parser


def test_frozen_baseline_catalog_is_packaged_and_contract_is_parseable():
    catalog = load_migration_catalog()

    assert [(migration.version, migration.name) for migration in catalog] == [
        (1, "initial_schema"),
        (2, "api_generation_scope"),
        (3, "api_idempotency"),
    ]
    assert catalog[0].checksum == BASELINE_SCHEMA_SHA256
    contract = _baseline_contract(catalog[0].sql)
    assert {
        "repositories",
        "index_snapshots",
        "review_runs",
        "review_publications",
    }.issubset(contract)
    assert {
        "update_description",
        "summary_comment_enabled",
        "fix_with_agent_enabled",
    }.issubset(contract["review_runs"])


def test_catalog_rejects_an_edited_baseline(tmp_path: Path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS changed (id BIGINT);\n"
    )

    with pytest.raises(MigrationDriftError, match="frozen version-1"):
        load_migration_catalog(tmp_path)


def test_catalog_requires_consecutive_strict_filenames(tmp_path: Path):
    baseline = Path("sql/schema.sql").read_bytes()
    assert hashlib.sha256(baseline).hexdigest() == BASELINE_SCHEMA_SHA256
    (tmp_path / "schema.sql").write_bytes(baseline)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0003_skipped.sql").write_text("SELECT 1;\n")

    with pytest.raises(MigrationError, match="consecutive"):
        load_migration_catalog(tmp_path)

    (migrations / "0003_skipped.sql").unlink()
    (migrations / "0002-invalid.sql").write_text("SELECT 1;\n")
    with pytest.raises(MigrationError, match="filename is invalid"):
        load_migration_catalog(tmp_path)


def test_catalog_rejects_non_transactional_migration_sql(tmp_path: Path):
    baseline = Path("sql/schema.sql").read_bytes()
    (tmp_path / "schema.sql").write_bytes(baseline)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_break_atomicity.sql").write_text(
        "CREATE TABLE unsafe (id BIGINT);\nCOMMIT;\n"
    )

    with pytest.raises(MigrationError, match="non-transactional"):
        load_migration_catalog(tmp_path)


def test_database_commands_are_available_from_the_unified_cli():
    parser = _parser()

    migrate = parser.parse_args(
        [
            "database",
            "migrate",
            "--adopt-existing",
            "--actor",
            "operator@example.com",
        ]
    )
    status = parser.parse_args(["database", "status"])
    verify = parser.parse_args(["database", "verify"])

    assert migrate.database_command == "migrate"
    assert migrate.adopt_existing
    assert migrate.actor == "operator@example.com"
    assert status.database_command == "status"
    assert verify.database_command == "verify"
