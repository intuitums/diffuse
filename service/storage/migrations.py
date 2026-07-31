"""Transactional, checksum-verified PostgreSQL schema migrations."""

from __future__ import annotations

import hashlib
import os
import re
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2.extensions

BASELINE_SCHEMA_SHA256 = (
    "d07a253ff0d134f0b05ebc738838d1645f532fc2d1365c7dab22f17fe7b21a32"
)
MIGRATION_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"diffuse:database-migrations:v1").digest()[:8],
    byteorder="big",
    signed=True,
)
MIGRATION_FILE_PATTERN = re.compile(
    r"^(?P<version>[0-9]{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$"
)
MIGRATION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
MIGRATION_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,254}$")
NON_TRANSACTIONAL_SQL_PATTERN = re.compile(
    r"(?:^|;)\s*(?:"
    r"ABORT|BEGIN|COMMIT|END|ROLLBACK|START\s+TRANSACTION|"
    r"VACUUM|CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY|"
    r"DROP\s+INDEX\s+CONCURRENTLY"
    r")\b",
    flags=re.IGNORECASE,
)
TABLE_DECLARATION_PATTERN = re.compile(
    r"^CREATE TABLE IF NOT EXISTS ([a-z][a-z0-9_]*) \($"
)
COLUMN_DECLARATION_PATTERN = re.compile(r"^    ([a-z][a-z0-9_]*)\s+")
MAX_MIGRATION_BYTES = 10 * 1024 * 1024
METADATA_TABLE = "diffuse_schema_migrations"
# Baseline columns a later migration deliberately removes. The contract below is
# parsed from the frozen version-1 schema, which cannot be edited, so without
# this every database that has correctly applied 0010 would be reported as
# missing columns it is supposed to have lost.
RETIRED_BASELINE_COLUMNS = frozenset(
    {
        "code_chunks.embedding",
        "index_snapshots.embedding_model",
        "index_snapshots.embedding_dimensions",
    }
)
_COLUMN_KEYWORDS = frozenset(
    {"check", "constraint", "foreign", "primary", "unique"}
)


class MigrationError(RuntimeError):
    """Base failure for an unsafe or incomplete database migration state."""


class MigrationDriftError(MigrationError):
    """Applied migration metadata no longer matches the immutable catalog."""


class UnversionedDatabaseError(MigrationError):
    """An existing schema requires explicit adoption before migrations run."""


class DatabaseNotCurrentError(MigrationError):
    """The database has unapplied migrations or violates the baseline contract."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str
    source_path: str

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("Migration version must be positive")
        if not MIGRATION_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("Migration name is invalid")
        if not MIGRATION_CHECKSUM_PATTERN.fullmatch(self.checksum):
            raise ValueError("Migration checksum is invalid")
        if not self.sql.strip() or "\x00" in self.sql:
            raise ValueError("Migration SQL must be non-empty UTF-8 text")


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    adopted: bool


@dataclass(frozen=True)
class MigrationStatus:
    state: str
    current_version: int
    latest_version: int
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]

    @property
    def current(self) -> bool:
        return (
            self.state == "versioned"
            and self.current_version == self.latest_version
            and not self.pending
        )


@dataclass(frozen=True)
class MigrationResult:
    before_version: int
    after_version: int
    applied: tuple[AppliedMigration, ...]
    adopted_baseline: bool


def _configured_sql_directory() -> Path:
    configured = os.environ.get("DIFFUSE_SQL_DIR")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            raise ValueError("DIFFUSE_SQL_DIR must be an absolute path")
        candidates = (candidate,)
    else:
        # service/storage/migrations.py -> service/storage -> service -> repository root.
        project_directory = Path(__file__).resolve().parent.parent.parent / "sql"
        data_directory = (
            Path(sysconfig.get_path("data")) / "share" / "diffuse" / "sql"
        )
        candidates = (project_directory, data_directory)
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "schema.sql").is_file():
            return resolved
    raise MigrationError("Diffuse migration SQL is not installed")


def _read_migration(path: Path, *, version: int, name: str) -> Migration:
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"Migration is not a regular file: {path.name}")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_MIGRATION_BYTES:
        raise MigrationError(
            f"Migration {path.name} must contain 1 to {MAX_MIGRATION_BYTES} bytes"
        )
    try:
        sql = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationError(f"Migration is not UTF-8: {path.name}") from error
    if "\x00" in sql:
        raise MigrationError(f"Migration contains a NUL byte: {path.name}")
    if any(line.lstrip().startswith("\\") for line in sql.splitlines()):
        raise MigrationError(
            f"Migration contains a psql-only command: {path.name}"
        )
    if NON_TRANSACTIONAL_SQL_PATTERN.search(sql):
        raise MigrationError(
            f"Migration contains transaction control or non-transactional DDL: "
            f"{path.name}"
        )
    checksum = hashlib.sha256(payload).hexdigest()
    return Migration(
        version=version,
        name=name,
        checksum=checksum,
        sql=sql,
        source_path=str(path),
    )


def load_migration_catalog(
    sql_directory: Path | None = None,
) -> tuple[Migration, ...]:
    """Load the frozen baseline and consecutive append-only migrations."""
    root = (sql_directory or _configured_sql_directory()).resolve()
    baseline = _read_migration(
        root / "schema.sql",
        version=1,
        name="initial_schema",
    )
    if baseline.checksum != BASELINE_SCHEMA_SHA256:
        raise MigrationDriftError(
            "sql/schema.sql is the frozen version-1 migration and was edited; "
            "add a numbered migration instead"
        )
    migrations = [baseline]
    migration_directory = root / "migrations"
    if migration_directory.exists():
        if migration_directory.is_symlink() or not migration_directory.is_dir():
            raise MigrationError("sql/migrations must be a regular directory")
        for path in sorted(migration_directory.iterdir()):
            if path.name.startswith(".") or path.suffix != ".sql":
                continue
            match = MIGRATION_FILE_PATTERN.fullmatch(path.name)
            if match is None:
                raise MigrationError(
                    f"Migration filename is invalid: {path.name}"
                )
            version = int(match.group("version"))
            migrations.append(
                _read_migration(
                    path,
                    version=version,
                    name=match.group("name"),
                )
            )
    versions = tuple(migration.version for migration in migrations)
    expected = tuple(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(
            "Migration versions must be unique and consecutive from 0001"
        )
    return tuple(migrations)


def _baseline_contract(sql: str) -> dict[str, frozenset[str]]:
    contract: dict[str, set[str]] = {}
    current_table: str | None = None
    for line in sql.splitlines():
        table_match = TABLE_DECLARATION_PATTERN.fullmatch(line)
        if table_match is not None:
            current_table = table_match.group(1)
            contract[current_table] = set()
            continue
        if current_table is None:
            continue
        if line == ");":
            current_table = None
            continue
        column_match = COLUMN_DECLARATION_PATTERN.match(line)
        if column_match is None:
            continue
        column = column_match.group(1)
        if column not in _COLUMN_KEYWORDS:
            contract[current_table].add(column)
    if not contract or any(not columns for columns in contract.values()):
        raise MigrationError("The baseline schema contract could not be parsed")
    return {
        table: frozenset(columns)
        for table, columns in contract.items()
    }


def _public_application_tables(cursor) -> frozenset[str]:
    cursor.execute(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
          AND tablename <> %s
        """,
        (METADATA_TABLE,),
    )
    return frozenset(str(row[0]) for row in cursor.fetchall())


def _verify_baseline_contract(cursor, baseline: Migration) -> None:
    expected = _baseline_contract(baseline.sql)
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        """
    )
    actual: dict[str, set[str]] = {}
    for table_name, column_name in cursor.fetchall():
        actual.setdefault(str(table_name), set()).add(str(column_name))
    missing_tables = sorted(set(expected) - set(actual))
    missing_columns = sorted(
        identity
        for table, columns in expected.items()
        for column in columns - actual.get(table, set())
        if (identity := f"{table}.{column}") not in RETIRED_BASELINE_COLUMNS
    )
    if missing_tables or missing_columns:
        details = []
        if missing_tables:
            details.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            details.append("missing columns: " + ", ".join(missing_columns))
        raise DatabaseNotCurrentError(
            "Database does not satisfy the version-1 schema contract ("
            + "; ".join(details)
            + ")"
        )


def _metadata_table_exists(cursor) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (f"public.{METADATA_TABLE}",))
    return cursor.fetchone()[0] is not None


def _create_metadata_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
            version       INTEGER PRIMARY KEY CHECK (version > 0),
            name          TEXT NOT NULL UNIQUE
                              CHECK (name ~ '^[a-z][a-z0-9_]{{0,99}}$'),
            checksum      TEXT NOT NULL
                              CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
            adopted       BOOLEAN NOT NULL DEFAULT FALSE,
            applied_by    TEXT NOT NULL
                              CHECK (length(applied_by) BETWEEN 1 AND 255),
            execution_ms  INTEGER NOT NULL CHECK (execution_ms >= 0),
            applied_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _read_applied(cursor) -> tuple[AppliedMigration, ...]:
    cursor.execute(
        f"""
        SELECT version, name, checksum, adopted
        FROM {METADATA_TABLE}
        ORDER BY version
        """
    )
    return tuple(
        AppliedMigration(
            version=int(row[0]),
            name=str(row[1]),
            checksum=str(row[2]),
            adopted=bool(row[3]),
        )
        for row in cursor.fetchall()
    )


def _validate_applied(
    applied: tuple[AppliedMigration, ...],
    catalog: tuple[Migration, ...],
) -> None:
    if tuple(item.version for item in applied) != tuple(
        range(1, len(applied) + 1)
    ):
        raise MigrationDriftError(
            "Applied migration history is not a consecutive prefix"
        )
    if len(applied) > len(catalog):
        raise MigrationDriftError(
            "Database migration version is newer than this Diffuse build"
        )
    for record, migration in zip(applied, catalog, strict=False):
        if (
            record.version != migration.version
            or record.name != migration.name
            or record.checksum != migration.checksum
        ):
            raise MigrationDriftError(
                f"Applied migration {record.version:04d} does not match "
                "the immutable Diffuse migration catalog"
            )


def migration_status(
    conn: psycopg2.extensions.connection,
    *,
    migrations: tuple[Migration, ...] | None = None,
) -> MigrationStatus:
    catalog = migrations or load_migration_catalog()
    with conn.cursor() as cursor:
        if not _metadata_table_exists(cursor):
            tables = _public_application_tables(cursor)
            return MigrationStatus(
                state="unversioned" if tables else "empty",
                current_version=0,
                latest_version=catalog[-1].version,
                applied=(),
                pending=catalog,
            )
        applied = _read_applied(cursor)
        _validate_applied(applied, catalog)
        tables = _public_application_tables(cursor)
    return MigrationStatus(
        state="versioned" if applied else ("unversioned" if tables else "empty"),
        current_version=applied[-1].version if applied else 0,
        latest_version=catalog[-1].version,
        applied=applied,
        pending=catalog[len(applied) :],
    )


def migrate_database(
    conn: psycopg2.extensions.connection,
    *,
    adopt_existing: bool = False,
    actor: str = "diffuse-migrator",
    migrations: tuple[Migration, ...] | None = None,
) -> MigrationResult:
    """Apply every pending migration atomically under a database-wide lock."""
    if not MIGRATION_ACTOR_PATTERN.fullmatch(actor):
        raise ValueError("Migration actor is invalid")
    catalog = migrations or load_migration_catalog()
    applied_now: list[AppliedMigration] = []
    adopted_baseline = False
    with conn, conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        existing_tables = _public_application_tables(cursor)
        _create_metadata_table(cursor)
        applied = _read_applied(cursor)
        _validate_applied(applied, catalog)
        before_version = applied[-1].version if applied else 0
        if not applied and existing_tables:
            if not adopt_existing:
                raise UnversionedDatabaseError(
                    "Database contains an unversioned Diffuse schema; inspect "
                    "it, then rerun with --adopt-existing"
                )
            _verify_baseline_contract(cursor, catalog[0])
            cursor.execute(
                f"""
                    INSERT INTO {METADATA_TABLE} (
                        version,
                        name,
                        checksum,
                        adopted,
                        applied_by,
                        execution_ms
                    )
                    VALUES (%s, %s, %s, TRUE, %s, 0)
                    """,
                (
                    catalog[0].version,
                    catalog[0].name,
                    catalog[0].checksum,
                    actor,
                ),
            )
            baseline_record = AppliedMigration(
                version=catalog[0].version,
                name=catalog[0].name,
                checksum=catalog[0].checksum,
                adopted=True,
            )
            applied = (baseline_record,)
            applied_now.append(baseline_record)
            adopted_baseline = True
        for migration in catalog[len(applied) :]:
            started = time.monotonic()
            cursor.execute(migration.sql)
            execution_ms = max(
                0,
                round((time.monotonic() - started) * 1000),
            )
            cursor.execute(
                f"""
                    INSERT INTO {METADATA_TABLE} (
                        version,
                        name,
                        checksum,
                        adopted,
                        applied_by,
                        execution_ms
                    )
                    VALUES (%s, %s, %s, FALSE, %s, %s)
                    """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    actor,
                    execution_ms,
                ),
            )
            record = AppliedMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                adopted=False,
            )
            applied += (record,)
            applied_now.append(record)
        _verify_baseline_contract(cursor, catalog[0])
        after_version = applied[-1].version if applied else 0
    return MigrationResult(
        before_version=before_version,
        after_version=after_version,
        applied=tuple(applied_now),
        adopted_baseline=adopted_baseline,
    )


def verify_database_current(
    conn: psycopg2.extensions.connection,
    *,
    migrations: tuple[Migration, ...] | None = None,
) -> MigrationStatus:
    catalog = migrations or load_migration_catalog()
    status = migration_status(conn, migrations=catalog)
    if not status.current:
        raise DatabaseNotCurrentError(
            f"Database schema is {status.state} at version "
            f"{status.current_version}; Diffuse requires version "
            f"{status.latest_version}"
        )
    with conn.cursor() as cursor:
        _verify_baseline_contract(cursor, catalog[0])
    return status
