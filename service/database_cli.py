"""Operator commands for Diffuse database schema migrations."""

from __future__ import annotations

import argparse
import json
from contextlib import closing

from indexer.store import get_conn
from service.database_migrations import (
    MigrationStatus,
    migrate_database,
    migration_status,
    verify_database_current,
)


def _status_json(status: MigrationStatus) -> dict[str, object]:
    return {
        "schema_version": "diffuse-database-status-v1",
        "state": status.state,
        "current": status.current,
        "current_version": status.current_version,
        "latest_version": status.latest_version,
        "applied": [
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": migration.checksum,
                "adopted": migration.adopted,
            }
            for migration in status.applied
        ],
        "pending": [
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": migration.checksum,
            }
            for migration in status.pending
        ],
    }


def _migrate(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        result = migrate_database(
            conn,
            adopt_existing=args.adopt_existing,
            actor=args.actor,
        )
        status = migration_status(conn)
    payload = _status_json(status)
    payload["before_version"] = result.before_version
    payload["after_version"] = result.after_version
    payload["adopted_baseline"] = result.adopted_baseline
    payload["applied_now"] = [
        {
            "version": migration.version,
            "name": migration.name,
            "checksum": migration.checksum,
            "adopted": migration.adopted,
        }
        for migration in result.applied
    ]
    print(json.dumps(payload, indent=2, sort_keys=True))


def _status(_args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        status = migration_status(conn)
    print(json.dumps(_status_json(status), indent=2, sort_keys=True))


def _verify(_args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        status = verify_database_current(conn)
    print(json.dumps(_status_json(status), indent=2, sort_keys=True))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(
        dest="database_command",
        required=True,
    )
    migrate = subparsers.add_parser(
        "migrate",
        help="Apply pending schema migrations under a PostgreSQL advisory lock",
    )
    migrate.add_argument(
        "--adopt-existing",
        action="store_true",
        help=(
            "Explicitly adopt an unversioned database only after its complete "
            "baseline table/column contract is verified"
        ),
    )
    migrate.add_argument(
        "--actor",
        default="diffuse-migrator",
        help="Operator identity recorded in the migration ledger (default: %(default)s)",
    )
    migrate.set_defaults(handler=_migrate)

    status_parser = subparsers.add_parser(
        "status",
        help="Inspect applied and pending schema versions without changing state",
    )
    status_parser.set_defaults(handler=_status)

    verify = subparsers.add_parser(
        "verify",
        help="Fail unless migration history and the baseline schema are current",
    )
    verify.set_defaults(handler=_verify)
