"""Repository registration and persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import psycopg2.extras

from service.scm import (
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
    )


def register_repository(
    conn,
    *,
    scm_provider: str,
    scm_base_url: str,
    full_name: str,
    default_branch: str,
) -> RegisteredRepository:
    if scm_provider != "github":
        raise ValueError("scm_provider must be github")
    base_url = normalize_base_url(scm_base_url, field_name="scm_base_url")
    name = validate_repository_name(full_name)
    branch = validate_default_branch(default_branch)
    clone_url = repository_clone_url(base_url, name)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO repositories (
                scm_provider,
                scm_base_url,
                full_name,
                default_branch,
                clone_url,
                enabled
            )
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (scm_provider, scm_base_url, full_name)
            DO UPDATE SET
                default_branch = EXCLUDED.default_branch,
                clone_url = EXCLUDED.clone_url,
                enabled = TRUE,
                updated_at = now()
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
                last_error_code
            """,
            (scm_provider, base_url, name, branch, clone_url),
        )
        return _repository_from_row(cursor.fetchone())


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
                last_error_code
            FROM repositories
            WHERE id = %s
            """,
            (repository_id,),
        )
        row = cursor.fetchone()
        if not row or not row["clone_url"] or not row["default_branch"]:
            return None
        return _repository_from_row(row)


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
                last_error_code
            FROM repositories
            WHERE clone_url IS NOT NULL
              AND default_branch IS NOT NULL
            ORDER BY scm_provider, scm_base_url, full_name
            """
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
