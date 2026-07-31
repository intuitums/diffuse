"""Shared bearer-token authentication for Diffuse HTTP surfaces."""

from __future__ import annotations

import os
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import anyio
import psycopg2

from indexer.store import get_conn
from service.hosted.api_tokens import (
    ADMIN_SCOPE,
    API_GENERATE_SCOPE,
    API_READ_SCOPE,
    API_WRITE_SCOPE,
    MCP_GENERATE_SCOPE,
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
    api_token_sha256,
    load_service_token_access,
    validate_api_token,
)

BOOTSTRAP_SCOPES = (
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
    MCP_GENERATE_SCOPE,
    API_READ_SCOPE,
    API_WRITE_SCOPE,
    API_GENERATE_SCOPE,
    ADMIN_SCOPE,
)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    client_id: str
    subject: str
    scopes: tuple[str, ...]
    auth_kind: str
    token_id: int | None
    all_repositories: bool
    repository_ids: tuple[int, ...]
    expires_at: datetime | None

    def authorized_repository_ids(self) -> frozenset[int] | None:
        if self.auth_kind not in {"bootstrap", "service_token"}:
            raise RuntimeError("Authorization context is invalid")
        if self.auth_kind == "bootstrap":
            if (
                self.token_id is not None
                or not self.all_repositories
                or self.repository_ids
            ):
                raise RuntimeError("Authorization context is invalid")
        elif self.token_id is None or self.token_id <= 0:
            raise RuntimeError("Authorization context is invalid")
        if self.all_repositories:
            if self.repository_ids:
                raise RuntimeError("Authorization context is invalid")
            return None
        if (
            not self.repository_ids
            or len(self.repository_ids) > 100
            or any(
                type(repository_id) is not int or repository_id <= 0
                for repository_id in self.repository_ids
            )
        ):
            raise RuntimeError("Authorization context is invalid")
        return frozenset(self.repository_ids)

    def has_scopes(self, *required: str) -> bool:
        return ADMIN_SCOPE in self.scopes or all(
            scope in self.scopes for scope in required
        )


def _load_service_access(token_sha256: str):
    with closing(get_conn()) as conn, conn:
        return load_service_token_access(conn, token_sha256=token_sha256)


async def authenticate_bearer_token(token: str) -> AuthenticatedPrincipal | None:
    """Authenticate the recovery credential or a durable service token."""
    try:
        validate_api_token(token)
    except ValueError:
        return None
    configured = os.environ.get("DIFFUSE_API_TOKEN", "")
    try:
        validate_api_token(configured)
    except ValueError:
        configured = ""
    if configured and secrets.compare_digest(token, configured):
        return AuthenticatedPrincipal(
            client_id="diffuse-self-hosted",
            subject="self-hosted-operator",
            scopes=BOOTSTRAP_SCOPES,
            auth_kind="bootstrap",
            token_id=None,
            all_repositories=True,
            repository_ids=(),
            expires_at=None,
        )
    try:
        access = await anyio.to_thread.run_sync(
            _load_service_access,
            api_token_sha256(token),
        )
    except (OSError, ValueError, psycopg2.Error):
        return None
    if access is None:
        return None
    return AuthenticatedPrincipal(
        client_id=f"diffuse-service-token-{access.id}",
        subject=access.name,
        scopes=access.scopes,
        auth_kind="service_token",
        token_id=access.id,
        all_repositories=access.all_repositories,
        repository_ids=access.repository_ids,
        expires_at=access.expires_at,
    )
