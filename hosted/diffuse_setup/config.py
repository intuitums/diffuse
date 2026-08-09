"""Fail-closed configuration for the small hosted Diffuse-Agent service."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

_INSTALLATION_ID = re.compile(r"^[0-9]{1,20}$")


class HostedConfigurationError(ValueError):
    """The integration service cannot safely start with its current configuration."""


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HostedConfigurationError(f"{name} must be configured")
    return value


def optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def public_url() -> str:
    value = required("DIFFUSE_SETUP_PUBLIC_URL").rstrip("/")
    if not value.startswith("https://") or "/" in value[len("https://") :]:
        raise HostedConfigurationError("DIFFUSE_SETUP_PUBLIC_URL must be an HTTPS origin")
    return value


def github_app_id() -> str:
    value = required("GITHUB_APP_ID")
    if not _INSTALLATION_ID.fullmatch(value):
        raise HostedConfigurationError("GITHUB_APP_ID must be numeric")
    return value


def github_private_key() -> str:
    value = required("GITHUB_APP_PRIVATE_KEY")
    if "BEGIN" not in value or "PRIVATE KEY" not in value:
        raise HostedConfigurationError("GITHUB_APP_PRIVATE_KEY must be a PEM private key")
    return value.replace("\\n", "\n")


def webhook_secret() -> str:
    return required("GITHUB_WEBHOOK_SECRET")


def database_url() -> str:
    # The Vercel Marketplace Neon integration manages DATABASE_URL for its
    # attached project. Keep the explicit name as an override for operators who
    # intentionally use another PostgreSQL provider, but do not force them to
    # copy a managed connection string into a second secret store.
    return optional("DIFFUSE_SETUP_DATABASE_URL") or required("DATABASE_URL")


def token_key() -> bytes:
    """Return the key that hashes instance credentials in the hosted database."""
    value = required("DIFFUSE_SETUP_TOKEN_PEPPER")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise HostedConfigurationError(
            "DIFFUSE_SETUP_TOKEN_PEPPER must be base64url text"
        ) from error
    if len(decoded) < 32:
        raise HostedConfigurationError("DIFFUSE_SETUP_TOKEN_PEPPER must decode to 32 bytes")
    return decoded


@dataclass(frozen=True)
class OAuthConfiguration:
    client_id: str
    client_secret: str


def oauth_configuration() -> OAuthConfiguration:
    return OAuthConfiguration(
        client_id=required("GITHUB_OAUTH_CLIENT_ID"),
        client_secret=required("GITHUB_OAUTH_CLIENT_SECRET"),
    )
