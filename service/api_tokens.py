"""Durable, non-recoverable service-token authorization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg2
import psycopg2.extras

MCP_READ_SCOPE = "diffuse:mcp:read"
MCP_WRITE_SCOPE = "diffuse:mcp:write"
MCP_GENERATE_SCOPE = "diffuse:mcp:generate"
API_READ_SCOPE = "diffuse:api:read"
API_WRITE_SCOPE = "diffuse:api:write"
API_GENERATE_SCOPE = "diffuse:api:generate"
ADMIN_SCOPE = "diffuse:admin"
ALLOWED_API_TOKEN_SCOPES = frozenset(
    {
        MCP_READ_SCOPE,
        MCP_WRITE_SCOPE,
        MCP_GENERATE_SCOPE,
        API_READ_SCOPE,
        API_WRITE_SCOPE,
        API_GENERATE_SCOPE,
        ADMIN_SCOPE,
    }
)
MIN_API_TOKEN_CHARS = 32
MAX_API_TOKEN_CHARS = 512
MAX_TOKEN_REPOSITORIES = 100
TOKEN_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")
ACTOR_PATTERN = re.compile(r"[^\x00-\x1f\x7f]{1,255}")


@dataclass(frozen=True)
class ServiceTokenAccess:
    id: int
    name: str
    scopes: tuple[str, ...]
    all_repositories: bool
    repository_ids: tuple[int, ...]
    expires_at: datetime | None


@dataclass(frozen=True)
class ServiceTokenRecord(ServiceTokenAccess):
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    revocation_reason: str | None


def validate_api_token(value: str) -> str:
    if (
        not MIN_API_TOKEN_CHARS <= len(value) <= MAX_API_TOKEN_CHARS
        or not value.isascii()
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError(
            f"Service tokens must contain {MIN_API_TOKEN_CHARS} to "
            f"{MAX_API_TOKEN_CHARS} visible ASCII characters"
        )
    return value


def api_token_sha256(value: str) -> str:
    return hashlib.sha256(validate_api_token(value).encode()).hexdigest()


def _name(value: str) -> str:
    normalized = value.strip()
    if not TOKEN_NAME_PATTERN.fullmatch(normalized):
        raise ValueError("Service-token names must contain 1 to 100 safe characters")
    return normalized


def _actor(value: str) -> str:
    normalized = value.strip()
    if not ACTOR_PATTERN.fullmatch(normalized):
        raise ValueError("Actor labels must contain 1 to 255 visible characters")
    return normalized


def _scopes(values: tuple[str, ...]) -> tuple[str, ...]:
    scopes = tuple(dict.fromkeys(values))
    if (
        not scopes
        or len(scopes) > 8
        or any(scope not in ALLOWED_API_TOKEN_SCOPES for scope in scopes)
    ):
        raise ValueError("Service-token scopes are invalid")
    return scopes


def _repository_ids(
    values: tuple[int, ...],
    *,
    all_repositories: bool,
) -> tuple[int, ...]:
    repository_ids = tuple(dict.fromkeys(values))
    if any(value <= 0 for value in repository_ids):
        raise ValueError("Repository IDs must be positive")
    if len(repository_ids) > MAX_TOKEN_REPOSITORIES:
        raise ValueError(
            f"Service tokens may reference at most {MAX_TOKEN_REPOSITORIES} repositories"
        )
    if all_repositories == bool(repository_ids):
        raise ValueError(
            "Choose either --all-repositories or one or more --repository-id values"
        )
    return repository_ids


def _record(row, repository_ids: tuple[int, ...]) -> ServiceTokenRecord:
    return ServiceTokenRecord(
        id=int(row["id"]),
        name=row["name"],
        scopes=tuple(row["scopes"]),
        all_repositories=bool(row["all_repositories"]),
        repository_ids=repository_ids,
        expires_at=row["expires_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        revocation_reason=row["revocation_reason"],
    )


def _audit(
    cursor,
    *,
    actor: str,
    action: str,
    token_id: int,
    details: dict[str, object],
) -> None:
    cursor.execute(
        """
        INSERT INTO audit_events (
            actor_kind,
            actor_label,
            action,
            resource_kind,
            resource_id,
            details
        )
        VALUES ('operator', %s, %s, 'api_token', %s, %s)
        """,
        (
            actor,
            action,
            str(token_id),
            psycopg2.extras.Json(details),
        ),
    )


def create_service_token(
    conn,
    *,
    name: str,
    token: str,
    scopes: tuple[str, ...],
    repository_ids: tuple[int, ...] = (),
    all_repositories: bool = False,
    actor: str,
    expires_at: datetime | None = None,
) -> ServiceTokenRecord:
    normalized_name = _name(name)
    token_hash = api_token_sha256(token)
    normalized_scopes = _scopes(scopes)
    normalized_repositories = _repository_ids(
        repository_ids,
        all_repositories=all_repositories,
    )
    normalized_actor = _actor(actor)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("Service-token expiration must include a timezone")
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= datetime.now(UTC):
            raise ValueError("Service-token expiration must be in the future")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            if normalized_repositories:
                cursor.execute(
                    """
                    SELECT id
                    FROM repositories
                    WHERE id = ANY(%s)
                    ORDER BY id
                    FOR SHARE
                    """,
                    (list(normalized_repositories),),
                )
                found = tuple(int(row["id"]) for row in cursor.fetchall())
                if found != tuple(sorted(normalized_repositories)):
                    raise ValueError("One or more token repositories do not exist")
            cursor.execute(
                """
                INSERT INTO api_tokens (
                    name,
                    token_sha256,
                    scopes,
                    all_repositories,
                    created_by,
                    expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    normalized_name,
                    token_hash,
                    list(normalized_scopes),
                    all_repositories,
                    normalized_actor,
                    expires_at,
                ),
            )
            row = cursor.fetchone()
            token_id = int(row["id"])
            for repository_id in normalized_repositories:
                cursor.execute(
                    """
                    INSERT INTO api_token_repositories (
                        api_token_id,
                        repository_id
                    )
                    VALUES (%s, %s)
                    """,
                    (token_id, repository_id),
                )
            _audit(
                cursor,
                actor=normalized_actor,
                action="api_token.created",
                token_id=token_id,
                details={
                    "name": normalized_name,
                    "scopes": list(normalized_scopes),
                    "all_repositories": all_repositories,
                    "repository_ids": list(normalized_repositories),
                    "expires_at": (
                        expires_at.isoformat() if expires_at is not None else None
                    ),
                },
            )
    except psycopg2.errors.UniqueViolation:
        raise ValueError("Service-token name or credential already exists") from None
    return _record(row, normalized_repositories)


def _token_repository_ids(cursor, token_id: int) -> tuple[int, ...]:
    cursor.execute(
        """
        SELECT repository_id
        FROM api_token_repositories
        WHERE api_token_id = %s
        ORDER BY repository_id
        """,
        (token_id,),
    )
    return tuple(int(row["repository_id"]) for row in cursor.fetchall())


def list_service_tokens(conn) -> tuple[ServiceTokenRecord, ...]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute("SELECT * FROM api_tokens ORDER BY id")
        rows = cursor.fetchall()
        return tuple(
            _record(row, _token_repository_ids(cursor, int(row["id"])))
            for row in rows
        )


def revoke_service_token(
    conn,
    *,
    token_id: int,
    actor: str,
    reason: str,
) -> ServiceTokenRecord:
    if token_id <= 0:
        raise ValueError("Service-token ID must be positive")
    normalized_actor = _actor(actor)
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 1000 or "\x00" in normalized_reason:
        raise ValueError("Revocation reason must contain 1 to 1000 characters")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE api_tokens
            SET revoked_at = now(),
                revoked_by = %s,
                revocation_reason = %s,
                updated_at = now()
            WHERE id = %s
              AND revoked_at IS NULL
            RETURNING *
            """,
            (normalized_actor, normalized_reason, token_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Active service token does not exist")
        repository_ids = _token_repository_ids(cursor, token_id)
        _audit(
            cursor,
            actor=normalized_actor,
            action="api_token.revoked",
            token_id=token_id,
            details={
                "name": row["name"],
                "reason": normalized_reason,
            },
        )
    return _record(row, repository_ids)


def load_service_token_access(
    conn,
    *,
    token_sha256: str,
) -> ServiceTokenAccess | None:
    if not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
        raise ValueError("Service-token hash is invalid")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT *
            FROM api_tokens
            WHERE token_sha256 = %s
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            FOR UPDATE
            """,
            (token_sha256,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        token_id = int(row["id"])
        repository_ids = _token_repository_ids(cursor, token_id)
        if not row["all_repositories"] and not repository_ids:
            return None
        cursor.execute(
            """
            UPDATE api_tokens
            SET last_used_at = now(),
                updated_at = now()
            WHERE id = %s
              AND (
                    last_used_at IS NULL
                    OR last_used_at < now() - interval '5 minutes'
                  )
            """,
            (token_id,),
        )
    return ServiceTokenAccess(
        id=token_id,
        name=row["name"],
        scopes=tuple(row["scopes"]),
        all_repositories=bool(row["all_repositories"]),
        repository_ids=repository_ids,
        expires_at=row["expires_at"],
    )
