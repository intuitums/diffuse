"""Repository registration and persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import psycopg2.extras

from diffuse.repository.scm import (
    normalize_base_url,
    validate_branch_name,
    validate_repository_name,
)


@dataclass(frozen=True)
class RegisteredRepository:
    id: int
    scm_provider: str
    scm_base_url: str
    full_name: str
    default_branch: str
    clone_url: str
    enabled: bool
    mirror_state: str
    last_fetched_sha: str | None
    last_error_code: str | None
    auto_review: bool = True
    github_repository_id: int | None = None


def validate_default_branch(value: str) -> str:
    return validate_branch_name(value)


def repository_clone_url(scm_base_url: str, full_name: str) -> str:
    base_url = normalize_base_url(scm_base_url, field_name="scm_base_url")
    repository_name = validate_repository_name(full_name)
    parsed = urlsplit(base_url)
    encoded_name = "/".join(quote(part, safe="._-") for part in repository_name.split("/"))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/{encoded_name}.git"


def validate_repository_origin_allowed(
    scm_provider: str,
    scm_base_url: str,
) -> str:
    """Require onboarding origins to be explicit before provider tokens can reach them."""
    if scm_provider != "github":
        raise ValueError("scm_provider must be github")
    primary_variable = "GITHUB_WEB_URL"
    allowed_variable = "GITHUB_ALLOWED_INSTANCES"
    default_origin = "https://github.com"

    requested = normalize_base_url(scm_base_url, field_name="scm_base_url")
    allowed = {
        normalize_base_url(
            os.environ.get(primary_variable, default_origin),
            field_name=primary_variable,
        )
    }
    configured_origins = [
        value.strip()
        for value in os.environ.get(allowed_variable, "").split(",")
        if value.strip()
    ]
    if len(configured_origins) > 15:
        raise ValueError(f"{allowed_variable} may contain at most 15 origins")
    allowed.update(
        normalize_base_url(value, field_name=allowed_variable)
        for value in configured_origins
    )
    if requested not in allowed:
        raise ValueError(
            f"scm_base_url is not configured in {primary_variable} or "
            f"{allowed_variable}"
        )
    return requested


def _repository_from_row(row) -> RegisteredRepository:
    return RegisteredRepository(
        id=int(row["id"]),
        scm_provider=row["scm_provider"],
        scm_base_url=row["scm_base_url"],
        full_name=row["full_name"],
        default_branch=row["default_branch"],
        clone_url=row["clone_url"],
        enabled=bool(row["enabled"]),
        mirror_state=row["mirror_state"],
        last_fetched_sha=row["last_fetched_sha"],
        last_error_code=row["last_error_code"],
        auto_review=bool(row["auto_review"]),
        github_repository_id=(
            int(row["github_repository_id"])
            if row["github_repository_id"] is not None
            else None
        ),
    )


def register_repository(
    conn,
    *,
    scm_provider: str,
    scm_base_url: str,
    full_name: str,
    default_branch: str,
    github_repository_id: int | None = None,
) -> RegisteredRepository:
    if scm_provider != "github":
        raise ValueError("scm_provider must be github")
    base_url = normalize_base_url(scm_base_url, field_name="scm_base_url")
    name = validate_repository_name(full_name)
    branch = validate_default_branch(default_branch)
    clone_url = repository_clone_url(base_url, name)
    if github_repository_id is not None and (
        not isinstance(github_repository_id, int)
        or isinstance(github_repository_id, bool)
        or github_repository_id <= 0
    ):
        raise ValueError("github_repository_id must be a positive integer")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO repositories (
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled,
                github_repository_id
            )
            VALUES (%s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (scm_provider, scm_base_url, full_name)
            DO UPDATE SET
                default_branch = EXCLUDED.default_branch,
                clone_url = EXCLUDED.clone_url,
                enabled = TRUE,
                github_repository_id = COALESCE(
                    repositories.github_repository_id,
                    EXCLUDED.github_repository_id
                ),
                updated_at = now()
            WHERE EXCLUDED.github_repository_id IS NULL
               OR repositories.github_repository_id IS NULL
               OR repositories.github_repository_id = EXCLUDED.github_repository_id
            RETURNING
                id,
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled,
                mirror_state,
                last_fetched_sha,
                last_error_code,
                auto_review,
                github_repository_id
            """,
            (scm_provider, base_url, name, branch, clone_url, github_repository_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RepositoryIdentityConflictError(
                "GitHub repository name belongs to a different repository identity"
            )
        return _repository_from_row(row)


def get_repository(conn, repository_id: int) -> RegisteredRepository | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled,
                mirror_state,
                last_fetched_sha,
                last_error_code,
                auto_review,
                github_repository_id
            FROM repositories
            WHERE id = %s
            """,
            (repository_id,),
        )
        row = cursor.fetchone()
        if not row or not row["clone_url"] or not row["default_branch"]:
            return None
        return _repository_from_row(row)


def get_repository_by_full_name(
    conn,
    full_name: str,
    *,
    scm_base_url: str | None = None,
) -> RegisteredRepository | None:
    """Resolve an operator-facing repository name to its internal identity.

    A repository name is ordinarily unique within a self-hosted instance. When
    an operator connects more than one GitHub host, ``--base-url`` makes the
    identity explicit rather than exposing Diffuse's database primary key.
    """
    name = validate_repository_name(full_name)
    base_url = (
        normalize_base_url(scm_base_url, field_name="scm_base_url")
        if scm_base_url is not None
        else None
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled,
                mirror_state,
                last_fetched_sha,
                last_error_code,
                auto_review,
                github_repository_id
            FROM repositories
            WHERE full_name = %s
              AND (%s::TEXT IS NULL OR scm_base_url = %s)
              AND clone_url IS NOT NULL
              AND default_branch IS NOT NULL
            ORDER BY scm_provider, scm_base_url
            LIMIT 2
            """,
            (name, base_url, base_url),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError(
            "Repository identity is ambiguous; pass --base-url to select its GitHub host"
        )
    return _repository_from_row(rows[0])


class RepositoryIdentityConflictError(RuntimeError):
    """A GitHub repository identity would collide with another local repository."""


def resolve_github_repository(
    conn,
    *,
    scm_base_url: str,
    github_repository_id: int,
    full_name: str,
    default_branch: str | None = None,
    actor_label: str = "github-webhook",
) -> RegisteredRepository | None:
    """Resolve a signed GitHub identity and refresh mutable repository metadata.

    Legacy rows are bound by their first same-name GitHub webhook. Once bound,
    a GitHub repository ID wins over the mutable name, preserving all local
    settings and history across a rename or ownership transfer.
    """
    if not isinstance(github_repository_id, int) or isinstance(github_repository_id, bool):
        raise ValueError("github_repository_id must be a positive integer")
    if github_repository_id <= 0:
        return get_repository_by_full_name(conn, full_name, scm_base_url=scm_base_url)
    name = validate_repository_name(full_name)
    base_url = normalize_base_url(scm_base_url, field_name="scm_base_url")
    branch = validate_default_branch(default_branch) if default_branch else None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, scm_provider, scm_base_url, full_name, default_branch,
                   clone_url, enabled, mirror_state, last_fetched_sha,
                   last_error_code, auto_review, github_repository_id
            FROM repositories
            WHERE scm_provider = 'github'
              AND scm_base_url = %s
              AND github_repository_id = %s
            FOR UPDATE
            """,
            (base_url, github_repository_id),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT id, scm_provider, scm_base_url, full_name, default_branch,
                       clone_url, enabled, mirror_state, last_fetched_sha,
                       last_error_code, auto_review, github_repository_id
                FROM repositories
                WHERE scm_provider = 'github'
                  AND scm_base_url = %s
                  AND full_name = %s
                  AND github_repository_id IS NULL
                FOR UPDATE
                """,
                (base_url, name),
            )
            row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT id FROM repositories
                WHERE scm_provider = 'github'
                  AND scm_base_url = %s
                  AND full_name = %s
                """,
                (base_url, name),
            )
            if cursor.fetchone() is not None:
                raise RepositoryIdentityConflictError(
                    "GitHub repository name belongs to a different repository identity"
                )
            return None

        cursor.execute(
            """
            SELECT id FROM repositories
            WHERE scm_provider = 'github'
              AND scm_base_url = %s
              AND full_name = %s
              AND id <> %s
            """,
            (base_url, name, int(row["id"])),
        )
        if cursor.fetchone() is not None:
            raise RepositoryIdentityConflictError(
                "GitHub repository rename conflicts with another configured repository"
            )
        clone_url = repository_clone_url(base_url, name)
        cursor.execute(
            """
            UPDATE repositories
            SET github_repository_id = %s,
                full_name = %s,
                clone_url = %s,
                default_branch = COALESCE(%s, default_branch),
                updated_at = now()
            WHERE id = %s
            RETURNING id, scm_provider, scm_base_url, full_name, default_branch,
                      clone_url, enabled, mirror_state, last_fetched_sha,
                      last_error_code, auto_review, github_repository_id
            """,
            (github_repository_id, name, clone_url, branch, int(row["id"])),
        )
        resolved = _repository_from_row(cursor.fetchone())
        if row["full_name"] != name:
            cursor.execute(
                """
                INSERT INTO audit_events (
                    actor_kind, actor_label, action, resource_kind, resource_id,
                    repository_id, details
                )
                VALUES ('system', %s, 'repository.renamed', 'repository', %s, %s, %s)
                """,
                (
                    actor_label,
                    str(resolved.id),
                    resolved.id,
                    psycopg2.extras.Json(
                        {
                            "previous_full_name": row["full_name"],
                            "full_name": name,
                            "github_repository_id": github_repository_id,
                        }
                    ),
                ),
            )
        return resolved


def list_repositories(conn) -> list[RegisteredRepository]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled,
                mirror_state,
                last_fetched_sha,
                last_error_code,
                auto_review,
                github_repository_id
            FROM repositories
            WHERE clone_url IS NOT NULL
              AND default_branch IS NOT NULL
            ORDER BY scm_provider, scm_base_url, full_name
            """
        )
        return [_repository_from_row(row) for row in cursor.fetchall()]


def list_skipped_policy_sources(conn) -> dict[int, list[str]]:
    """Guidance sources discovery skipped, per repository, from each active snapshot."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT repository_id, skipped_policy_sources
            FROM index_snapshots
            WHERE status = 'active'
              AND skipped_policy_sources <> '[]'::jsonb
            """
        )
        return {
            int(repository_id): list(sources)
            for repository_id, sources in cursor.fetchall()
        }


def list_unbound_github_repositories(
    conn,
    *,
    limit: int,
) -> list[RegisteredRepository]:
    """Return a bounded batch of legacy records awaiting immutable ID binding."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, scm_provider, scm_base_url, full_name, default_branch,
                   clone_url, enabled, mirror_state, last_fetched_sha,
                   last_error_code, auto_review, github_repository_id
            FROM repositories
            WHERE scm_provider = 'github'
              AND github_repository_id IS NULL
              AND clone_url IS NOT NULL
              AND default_branch IS NOT NULL
            ORDER BY id
            LIMIT %s
            """,
            (limit,),
        )
        return [_repository_from_row(row) for row in cursor.fetchall()]


def set_repository_enabled(conn, repository_id: int, enabled: bool) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE repositories
            SET enabled = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (enabled, repository_id),
        )
        changed = cursor.rowcount == 1
        if changed and not enabled:
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET status = 'cancelled',
                    completed_at = now(),
                    updated_at = now()
                WHERE repository_id = %s
                  AND status = 'queued'
                """,
                (repository_id,),
            )
        return changed


def set_repository_auto_review(
    conn,
    repository_id: int,
    auto_review: bool,
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE repositories
            SET auto_review = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (auto_review, repository_id),
        )
        return cursor.rowcount == 1


def update_mirror_state(
    conn,
    repository_id: int,
    *,
    state: str,
    commit_sha: str | None = None,
    error_code: str | None = None,
) -> None:
    if state not in {"unconfigured", "syncing", "ready", "failed"}:
        raise ValueError("Invalid mirror state")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE repositories
            SET mirror_state = %s,
                last_fetched_sha = CASE
                    WHEN %s IS NOT NULL THEN %s
                    ELSE last_fetched_sha
                END,
                last_fetched_at = CASE
                    WHEN %s IS NOT NULL THEN now()
                    ELSE last_fetched_at
                END,
                last_error_code = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                state,
                commit_sha,
                commit_sha,
                commit_sha,
                error_code,
                repository_id,
            ),
        )
