"""Operator CLI for explicit repository onboarding and lifecycle management."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from contextlib import closing
from datetime import UTC, datetime

from diffuse.github.repository import fetch_github_repository_metadata
from diffuse.repository.indexing.store import get_conn
from diffuse.repository.mirror import RepositoryMirror
from diffuse.repository.registry import (
    RegisteredRepository,
    get_repository_by_full_name,
    list_repositories,
    list_skipped_policy_sources,
    register_repository,
    resolve_github_repository,
    set_repository_auto_review,
    set_repository_enabled,
    update_mirror_state,
    validate_repository_origin_allowed,
)
from diffuse.repository.sync import repository_index_event
from diffuse.review.workflow import enqueue_repository_index_event


def _github_repository_metadata(
    repository: RegisteredRepository,
) -> tuple[int, str, str]:
    """Compatibility seam for CLI tests; metadata ownership lives in github/."""
    metadata = fetch_github_repository_metadata(repository)
    return metadata.repository_id, metadata.full_name, metadata.default_branch


def _refresh_github_repository_identity(
    repository: RegisteredRepository,
) -> RegisteredRepository:
    github_repository_id, full_name, default_branch = _github_repository_metadata(
        repository
    )
    with closing(get_conn()) as conn, conn:
        resolved = resolve_github_repository(
            conn,
            scm_base_url=repository.scm_base_url,
            github_repository_id=github_repository_id,
            full_name=full_name,
            default_branch=default_branch,
        )
    if resolved is None:
        raise RuntimeError("GitHub repository is not registered in Diffuse")
    return resolved


def enqueue_initial_index(
    repository: RegisteredRepository,
    *,
    refresh_identity: bool = True,
) -> tuple[int, str]:
    if refresh_identity:
        repository = _refresh_github_repository_identity(repository)
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
    provisional = RegisteredRepository(
        id=0,
        scm_provider=args.provider,
        scm_base_url=args.base_url,
        full_name=args.repo,
        default_branch=args.default_branch,
        clone_url="https://invalid.example/not-used.git",
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    github_repository_id, full_name, default_branch = _github_repository_metadata(
        provisional
    )
    with closing(get_conn()) as conn, conn:
        repository = register_repository(
            conn,
            scm_provider=args.provider,
            scm_base_url=args.base_url,
            full_name=full_name,
            default_branch=default_branch,
            github_repository_id=github_repository_id,
        )
    print(
        f"Registered repository: {repository.scm_provider} {repository.full_name}"
    )
    if not args.no_initial_index:
        job_id, commit_sha = enqueue_initial_index(repository, refresh_identity=False)
        print(f"Queued initial index job {job_id} for {commit_sha[:12]}.")


def _repository_from_cli_target(args: argparse.Namespace) -> RegisteredRepository:
    with closing(get_conn()) as conn:
        repository = get_repository_by_full_name(
            conn,
            args.repository,
            scm_base_url=args.base_url,
        )
    if repository is None:
        raise ValueError("Repository does not exist or is not configured")
    return repository


def _reindex_repository(args: argparse.Namespace) -> None:
    if args.all:
        if args.repository is not None:
            raise ValueError("Pass either a repository name or --all, not both")
        if args.base_url is not None:
            raise ValueError("--base-url requires an owner/repo repository name")
        _reindex_every_repository()
        return
    if args.repository is None:
        raise ValueError(
            "Pass an owner/repo name, or --all to reindex every enabled repository"
        )
    repository = _repository_from_cli_target(args)
    if not repository.enabled:
        raise ValueError("Repository is disabled")
    job_id, commit_sha = enqueue_initial_index(repository)
    print(f"Queued index job {job_id} for {commit_sha[:12]}.")


def _reindex_every_repository() -> None:
    """Queue a fresh index for every enabled repository.

    A release that changes ``INDEX_FORMAT_VERSION`` -- a language-adapter schema
    change, a Tree-sitter grammar upgrade, or a policy schema change -- makes
    every existing snapshot incompatible. Retrieval refuses an incompatible
    snapshot, so until each repository is re-indexed its reviews fail closed
    with ``MissingRepositoryIndexError`` rather than quietly losing context.

    This is an explicit operator-maintenance operation. It is safe to re-run:
    indexing is keyed on the exact resolved commit and queued work is deduplicated.
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
            print(f"{repository.full_name}: could not queue: {error}")
            continue
        print(
            f"{repository.full_name}: queued index job {job_id} for {commit_sha[:12]}."
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
        skipped_sources = list_skipped_policy_sources(conn)
    output = [
        {
            "provider": repository.scm_provider,
            "base_url": repository.scm_base_url,
            "full_name": repository.full_name,
            "default_branch": repository.default_branch,
            "enabled": repository.enabled,
            "auto_review": repository.auto_review,
            "mirror_state": repository.mirror_state,
            "last_fetched_sha": repository.last_fetched_sha,
            "last_error_code": repository.last_error_code,
            "skipped_policy_sources": skipped_sources.get(repository.id, []),
        }
        for repository in repositories
    ]
    print(json.dumps(output, indent=2))


def _set_enabled(args: argparse.Namespace, *, enabled: bool) -> None:
    repository = _repository_from_cli_target(args)
    with closing(get_conn()) as conn, conn:
        changed = set_repository_enabled(conn, repository.id, enabled)
    if not changed:
        raise ValueError("Repository does not exist")
    state = "enabled" if enabled else "disabled"
    print(f"Repository {repository.full_name} is {state}.")


def _show_settings(args: argparse.Namespace) -> None:
    repository = _repository_from_cli_target(args)
    print(
        json.dumps(
            {
                "repository": repository.full_name,
                "enabled": repository.enabled,
                "auto_review": repository.auto_review,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _set_auto_review(args: argparse.Namespace) -> None:
    repository = _repository_from_cli_target(args)
    auto_review = args.auto_review == "on"
    with closing(get_conn()) as conn, conn:
        changed = set_repository_auto_review(conn, repository.id, auto_review)
    if not changed:
        raise ValueError("Repository does not exist")
    state = "on" if auto_review else "off"
    print(f"Automatic pull-request review for {repository.full_name} is {state}.")


def _add_repository_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "repository",
        metavar="OWNER/REPO",
        help="Repository full name as it appears on GitHub",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        help="GitHub host when the same owner/repo exists on more than one host",
    )


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
        choices=("github",),
        required=True,
        help="Source-control provider hosting the repository",
    )
    add_parser.add_argument(
        "--base-url",
        required=True,
        metavar="URL",
        help="SCM base URL, for example https://github.com or https://github.example.com",
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
        "repository",
        metavar="OWNER/REPO",
        help="Repository full name as it appears on GitHub",
    )
    disable_parser.add_argument(
        "--base-url",
        metavar="URL",
        help="GitHub host when the same owner/repo exists on more than one host",
    )
    disable_parser.set_defaults(handler=lambda args: _set_enabled(args, enabled=False))

    enable_parser = subparsers.add_parser(
        "enable",
        help="Re-enable an onboarded repository",
    )
    enable_parser.add_argument(
        "repository",
        metavar="OWNER/REPO",
        help="Repository full name as it appears on GitHub",
    )
    enable_parser.add_argument(
        "--base-url",
        metavar="URL",
        help="GitHub host when the same owner/repo exists on more than one host",
    )
    enable_parser.set_defaults(handler=lambda args: _set_enabled(args, enabled=True))

    settings_parser = subparsers.add_parser(
        "settings",
        help="View or change repository review settings",
    )
    settings_subparsers = settings_parser.add_subparsers(
        dest="repository_settings_command",
        required=True,
    )
    settings_show = settings_subparsers.add_parser(
        "show",
        help="Show a repository's effective settings",
    )
    _add_repository_target_arguments(settings_show)
    settings_show.set_defaults(handler=_show_settings)

    settings_set = settings_subparsers.add_parser(
        "set",
        help="Change a repository setting",
    )
    _add_repository_target_arguments(settings_set)
    settings_set.add_argument(
        "--auto-review",
        choices=("on", "off"),
        required=True,
        help="Automatically review eligible pull requests",
    )
    settings_set.set_defaults(handler=_set_auto_review)
