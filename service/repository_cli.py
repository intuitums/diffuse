"""Operator CLI for explicit repository onboarding and lifecycle management."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from contextlib import closing
from datetime import UTC, datetime

from indexer.store import get_conn
from service.repositories import (
    RegisteredRepository,
    get_repository,
    list_repositories,
    register_repository,
    set_repository_enabled,
    update_mirror_state,
    validate_repository_origin_allowed,
)
from service.repository_indexing import repository_index_event
from service.repository_mirror import RepositoryMirror
from service.workflow import enqueue_repository_index_event


def enqueue_initial_index(repository: RegisteredRepository) -> tuple[int, str]:
    with closing(get_conn()) as conn, conn:
        update_mirror_state(conn, repository.id, state="syncing")
    try:
        commit_sha = RepositoryMirror(repository).resolve_default_commit()
    except Exception:
        with closing(get_conn()) as conn, conn:
            update_mirror_state(
                conn,
                repository.id,
                state="failed",
                error_code="mirror_sync_failed",
            )
        raise

    with closing(get_conn()) as conn, conn:
        update_mirror_state(
            conn,
            repository.id,
            state="ready",
            commit_sha=commit_sha,
        )
        event = repository_index_event(
            repository,
            commit_sha=commit_sha,
            requested_at=datetime.now(UTC),
            delivery_id=f"manual-{uuid.uuid4()}",
        )
        serialized = json.dumps(
            event.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        result = enqueue_repository_index_event(
            conn,
            event,
            payload_sha256=hashlib.sha256(serialized).hexdigest(),
        )
    if result.job_id is None:
        raise RuntimeError(f"Initial index was not queued: {result.state}")
    return result.job_id, commit_sha


def _add_repository(args: argparse.Namespace) -> None:
    validate_repository_origin_allowed(args.provider, args.base_url)
    with closing(get_conn()) as conn, conn:
        repository = register_repository(
            conn,
            scm_provider=args.provider,
            scm_base_url=args.base_url,
            full_name=args.repo,
            default_branch=args.default_branch,
        )
    print(
        f"Registered repository {repository.id}: {repository.scm_provider} {repository.full_name}"
    )
    if not args.no_initial_index:
        job_id, commit_sha = enqueue_initial_index(repository)
        print(f"Queued initial index job {job_id} for {commit_sha[:12]}.")


def _sync_repository(args: argparse.Namespace) -> None:
    if args.all:
        if args.repository_id is not None:
            raise ValueError("Pass either a repository_id or --all, not both")
        _sync_every_repository()
        return
    if args.repository_id is None:
        raise ValueError(
            "Pass a repository_id, or --all to reindex every enabled repository"
        )
    with closing(get_conn()) as conn:
        repository = get_repository(conn, args.repository_id)
    if repository is None:
        raise ValueError("Repository does not exist or is not configured")
    if not repository.enabled:
        raise ValueError("Repository is disabled")
    job_id, commit_sha = enqueue_initial_index(repository)
    print(f"Queued index job {job_id} for {commit_sha[:12]}.")


def _sync_every_repository() -> None:
    """Queue a fresh index for every enabled repository.

    A release that changes ``INDEX_FORMAT_VERSION`` -- a language-adapter schema
    change, a Tree-sitter grammar upgrade, or a policy schema change -- makes
    every existing snapshot incompatible. Retrieval refuses an incompatible
    snapshot, so until each repository is re-indexed its reviews fail closed
    with ``MissingRepositoryIndexError`` rather than quietly losing context.

    This is the operator's post-upgrade step. It is safe to re-run: indexing is
    keyed on the exact resolved commit and queued work is deduplicated.
    """
    with closing(get_conn()) as conn:
        repositories = [
            repository for repository in list_repositories(conn) if repository.enabled
        ]
    if not repositories:
        print("No enabled repositories to reindex.")
        return
    failures = 0
    for repository in repositories:
        try:
            job_id, commit_sha = enqueue_initial_index(repository)
        except Exception as error:  # noqa: BLE001
            # One unreachable mirror must not abandon the rest of the sweep.
            failures += 1
            print(f"{repository.id} {repository.full_name}: could not queue: {error}")
            continue
        print(
            f"{repository.id} {repository.full_name}: "
            f"queued index job {job_id} for {commit_sha[:12]}."
        )
    queued = len(repositories) - failures
    print(f"Queued {queued} of {len(repositories)} enabled repositories.")
    if failures:
        raise RuntimeError(
            f"{failures} of {len(repositories)} repositories could not be queued"
        )


def _list_registered_repositories(_args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        repositories = list_repositories(conn)
    output = [
        {
            "id": repository.id,
            "provider": repository.scm_provider,
            "base_url": repository.scm_base_url,
            "full_name": repository.full_name,
            "default_branch": repository.default_branch,
            "enabled": repository.enabled,
            "mirror_state": repository.mirror_state,
            "last_fetched_sha": repository.last_fetched_sha,
            "last_error_code": repository.last_error_code,
        }
        for repository in repositories
    ]
    print(json.dumps(output, indent=2))


def _set_enabled(args: argparse.Namespace, *, enabled: bool) -> None:
    with closing(get_conn()) as conn, conn:
        changed = set_repository_enabled(conn, args.repository_id, enabled)
    if not changed:
        raise ValueError("Repository does not exist")
    state = "enabled" if enabled else "disabled"
    print(f"Repository {args.repository_id} is {state}.")


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(
        dest="repository_command",
        required=True,
    )

    add_parser = subparsers.add_parser(
        "add",
        help="Register a repository and queue its initial index",
    )
    add_parser.add_argument(
        "--provider",
        choices=("github", "gitlab"),
        required=True,
        help="Source-control provider hosting the repository",
    )
    add_parser.add_argument(
        "--base-url",
        required=True,
        metavar="URL",
        help="SCM base URL, for example https://github.com or https://gitlab.example.com",
    )
    add_parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="Repository full name as it appears on the provider",
    )
    add_parser.add_argument(
        "--default-branch",
        required=True,
        metavar="BRANCH",
        help="Branch to index and to use as the default review base, for example main",
    )
    add_parser.add_argument(
        "--no-initial-index",
        action="store_true",
        help="Register the repository without queueing the initial index job",
    )
    add_parser.set_defaults(handler=_add_repository)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Fetch and queue the current default-branch commit",
    )
    sync_parser.add_argument(
        "repository_id",
        type=int,
        nargs="?",
        help=(
            "Numeric repository id from `diffuse repository list`; "
            "omit when using --all"
        ),
    )
    sync_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Reindex every enabled repository. Required after an upgrade that "
            "changes the index format, which makes existing snapshots "
            "incompatible"
        ),
    )
    sync_parser.set_defaults(handler=_sync_repository)

    list_parser = subparsers.add_parser(
        "list",
        help="List registered repositories and mirror state",
    )
    list_parser.set_defaults(handler=_list_registered_repositories)

    disable_parser = subparsers.add_parser(
        "disable",
        help="Disable indexing and review for a repository",
    )
    disable_parser.add_argument(
        "repository_id",
        type=int,
        help="Numeric repository id from `diffuse repository list`",
    )
    disable_parser.set_defaults(handler=lambda args: _set_enabled(args, enabled=False))

    enable_parser = subparsers.add_parser(
        "enable",
        help="Re-enable an onboarded repository",
    )
    enable_parser.add_argument(
        "repository_id",
        type=int,
        help="Numeric repository id from `diffuse repository list`",
    )
    enable_parser.set_defaults(handler=lambda args: _set_enabled(args, enabled=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diffuse-repository")
    configure_parser(parser)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
