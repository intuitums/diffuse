"""Operator lifecycle commands for non-recoverable service tokens."""

from __future__ import annotations

import argparse
import json
import os
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta

from indexer.store import get_conn
from service.api_tokens import (
    ALLOWED_API_TOKEN_SCOPES,
    ServiceTokenRecord,
    create_service_token,
    list_service_tokens,
    revoke_service_token,
)

ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _record_json(record: ServiceTokenRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "scopes": list(record.scopes),
        "all_repositories": record.all_repositories,
        "repository_ids": list(record.repository_ids),
        "created_by": record.created_by,
        "created_at": _timestamp(record.created_at),
        "expires_at": _timestamp(record.expires_at),
        "last_used_at": _timestamp(record.last_used_at),
        "revoked_at": _timestamp(record.revoked_at),
        "revoked_by": record.revoked_by,
        "revocation_reason": record.revocation_reason,
    }


def _token_from_environment(variable_name: str) -> str:
    if not ENVIRONMENT_NAME_PATTERN.fullmatch(variable_name):
        raise ValueError("Token environment variable name is invalid")
    token = os.environ.get(variable_name)
    if token is None:
        raise ValueError(f"Token environment variable is not set: {variable_name}")
    return token


def _add_token(args: argparse.Namespace) -> None:
    expires_at = None
    if args.expires_in_days is not None:
        if not 1 <= args.expires_in_days <= 3650:
            raise ValueError("--expires-in-days must be between 1 and 3650")
        expires_at = datetime.now(UTC) + timedelta(days=args.expires_in_days)
    with closing(get_conn()) as conn, conn:
        record = create_service_token(
            conn,
            name=args.name,
            token=_token_from_environment(args.token_env),
            scopes=tuple(args.scope),
            repository_ids=tuple(args.repository_id or ()),
            all_repositories=args.all_repositories,
            actor=args.actor,
            expires_at=expires_at,
        )
    print(json.dumps(_record_json(record), indent=2, sort_keys=True))


def _list_tokens(_args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        records = list_service_tokens(conn)
    print(
        json.dumps(
            [_record_json(record) for record in records],
            indent=2,
            sort_keys=True,
        )
    )


def _revoke_token(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        record = revoke_service_token(
            conn,
            token_id=args.token_id,
            actor=args.actor,
            reason=args.reason,
        )
    print(json.dumps(_record_json(record), indent=2, sort_keys=True))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="token_command", required=True)

    add = subparsers.add_parser(
        "add",
        help="Store a hashed service token read from an environment variable",
    )
    add.add_argument("name")
    add.add_argument("--token-env", required=True)
    add.add_argument(
        "--scope",
        action="append",
        choices=sorted(ALLOWED_API_TOKEN_SCOPES),
        required=True,
    )
    repositories = add.add_mutually_exclusive_group(required=True)
    repositories.add_argument("--all-repositories", action="store_true")
    repositories.add_argument(
        "--repository-id",
        action="append",
        type=int,
    )
    add.add_argument("--actor", required=True)
    add.add_argument("--expires-in-days", type=int)
    add.set_defaults(handler=_add_token)

    list_parser = subparsers.add_parser(
        "list",
        help="List token metadata without credential hashes",
    )
    list_parser.set_defaults(handler=_list_tokens)

    revoke = subparsers.add_parser(
        "revoke",
        help="Irreversibly revoke an active service token",
    )
    revoke.add_argument("token_id", type=int)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.set_defaults(handler=_revoke_token)
