"""Audited repository onboarding and exact-commit indexing actions."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import psycopg2.extras

from service.repositories import (
    RegisteredRepository,
    get_repository,
    register_repository,
    repository_clone_url,
    validate_default_branch,
    validate_repository_origin_allowed,
)
from service.scm import PushEvent, validate_repository_name
from service.workflow import enqueue_repository_index_event


class RepositoryConfigurationConflictError(ValueError):
    """An existing repository has incompatible lifecycle configuration."""


class RepositoryAccessNotFoundError(LookupError):
    """A repository is absent or hidden by the caller's repository grants."""


def _advisory_lock_id(identity: str) -> int:
    return int.from_bytes(
        hashlib.sha256(identity.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _validate_actor(
    *,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
) -> None:
    if not actor_label or len(actor_label) > 255 or "\x00" in actor_label:
        raise ValueError("Repository action actor is invalid")
    if actor_kind == "service_token":
        if actor_token_id is None or actor_token_id <= 0:
            raise ValueError("Repository action actor is invalid")
    elif actor_kind != "operator" or actor_token_id is not None:
        raise ValueError("Repository action actor is invalid")


def register_repository_for_api(
    conn,
    *,
    scm_provider: str,
    scm_base_url: str,
    full_name: str,
    default_branch: str,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
) -> tuple[RegisteredRepository, bool]:
    """Create one enabled repository without silently replacing its configuration."""
    _validate_actor(
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )
    if scm_provider not in {"github", "gitlab"}:
        raise ValueError("scm_provider must be github or gitlab")
    base_url = validate_repository_origin_allowed(scm_provider, scm_base_url)
    name = validate_repository_name(full_name)
    branch = validate_default_branch(default_branch)
    clone_url = repository_clone_url(base_url, name)
    identity = f"{scm_provider}:{base_url}:{name}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_advisory_lock_id(f"repository:onboard:{identity}"),),
        )
        cursor.execute(
            """
            SELECT id, default_branch, clone_url, enabled
            FROM repositories
            WHERE scm_provider = %s
              AND scm_base_url = %s
              AND full_name = %s
            FOR UPDATE
            """,
            (scm_provider, base_url, name),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if (
                existing["default_branch"] != branch
                or existing["clone_url"] != clone_url
                or not bool(existing["enabled"])
            ):
                raise RepositoryConfigurationConflictError(
                    "Repository already exists with different or disabled configuration"
                )
            repository = get_repository(conn, int(existing["id"]))
            if repository is None:
                raise RuntimeError("Configured repository could not be reloaded")
            return repository, False

        repository = register_repository(
            conn,
            scm_provider=scm_provider,
            scm_base_url=base_url,
            full_name=name,
            default_branch=branch,
        )
        cursor.execute(
            """
            INSERT INTO audit_events (
                actor_kind,
                actor_label,
                action,
                resource_kind,
                resource_id,
                repository_id,
                details
            )
            VALUES (
                %s,
                %s,
                'repository.onboarded',
                'repository',
                %s,
                %s,
                %s
            )
            """,
            (
                actor_kind,
                actor_label,
                str(repository.id),
                repository.id,
                psycopg2.extras.Json(
                    {
                        "provider": repository.scm_provider,
                        "base_url": repository.scm_base_url,
                        "full_name": repository.full_name,
                        "default_branch": repository.default_branch,
                    }
                ),
            ),
        )
    return repository, True


def get_repository_for_index_action(
    conn,
    *,
    repository_id: int,
    authorized_repository_ids: frozenset[int] | None,
) -> RegisteredRepository:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise RepositoryAccessNotFoundError(
            "Repository does not exist or is not authorized"
        )
    repository = get_repository(conn, repository_id)
    if repository is None:
        raise RepositoryAccessNotFoundError(
            "Repository does not exist or is not authorized"
        )
    if not repository.enabled:
        raise RepositoryConfigurationConflictError("Repository is disabled")
    return repository


def enqueue_repository_index_trigger(
    conn,
    *,
    event: PushEvent,
    repository_id: int,
    authorized_repository_ids: frozenset[int] | None,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
) -> dict[str, object]:
    """Enqueue one provider-neutral index delivery and audit it exactly once."""
    _validate_actor(
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )
    repository = get_repository_for_index_action(
        conn,
        repository_id=repository_id,
        authorized_repository_ids=authorized_repository_ids,
    )
    if (
        repository.scm_provider != event.provider
        or repository.scm_base_url != event.scm_base_url
        or repository.full_name != event.repo_full_name
        or repository.default_branch != event.default_branch
    ):
        raise RepositoryConfigurationConflictError(
            "Index request no longer matches repository configuration"
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
    if not result.state.startswith("duplicate_delivery:"):
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (
                    actor_kind,
                    actor_label,
                    action,
                    resource_kind,
                    resource_id,
                    repository_id,
                    details
                )
                VALUES (
                    %s,
                    %s,
                    'repository.index_requested',
                    'repository_index',
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    actor_kind,
                    actor_label,
                    event.delivery_id,
                    repository.id,
                    psycopg2.extras.Json(
                        {
                            "commit_sha": event.after_sha,
                            "job_id": result.job_id,
                            "queue_state": result.state,
                        }
                    ),
                ),
            )
    return {
        "success": result.job_id is not None,
        "message": (
            "Repository index requested successfully"
            if result.job_id is not None
            else "Repository index request was recorded without a new job"
        ),
        "repository": {
            "id": repository.id,
            "name": repository.full_name,
            "remote": repository.scm_provider,
            "remoteUrl": repository.scm_base_url,
            "defaultBranch": repository.default_branch,
        },
        "beforeSha": event.before_sha,
        "commitSha": event.after_sha,
        "jobId": result.job_id,
        "queueState": result.state,
    }
