"""Fail-closed configuration for the GitHub Integration Service."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

_INSTALLATION_ID = re.compile(r"^[0-9]{1,20}$")

# Production Infisical/Vercel still carries the pre-rename DIFFUSE_SETUP_* names
# from the first hosted control-plane deploy. Prefer the current names, then
# fall back so a rename-only code deploy does not strand a live App.
_PUBLIC_URL_NAMES = (
    "DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL",
    "DIFFUSE_SETUP_PUBLIC_URL",
)
_DATABASE_URL_NAMES = (
    "DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL",
    "DIFFUSE_SETUP_DATABASE_URL",
)
_TOKEN_PEPPER_NAMES = (
    "DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER",
    "DIFFUSE_SETUP_TOKEN_PEPPER",
)


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


def _first_configured(*names: str) -> tuple[str, str] | None:
    for name in names:
        value = optional(name)
        if value is not None:
            return name, value
    return None


def _required_any(*names: str) -> tuple[str, str]:
    found = _first_configured(*names)
    if found is None:
        raise HostedConfigurationError(f"{names[0]} must be configured")
    return found


def public_url() -> str:
    name, value = _required_any(*_PUBLIC_URL_NAMES)
    value = value.rstrip("/")
    if not value.startswith("https://") or "/" in value[len("https://") :]:
        raise HostedConfigurationError(f"{name} must be an HTTPS origin")
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
    found = _first_configured(*_DATABASE_URL_NAMES)
    if found is not None:
        return found[1]
    return required("DATABASE_URL")


def token_key() -> bytes:
    """Return the key that hashes instance credentials in the hosted database."""
    name, value = _required_any(*_TOKEN_PEPPER_NAMES)
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise HostedConfigurationError(f"{name} must be base64url text") from error
    if len(decoded) < 32:
        raise HostedConfigurationError(f"{name} must decode to at least 32 bytes")
    return decoded


CREDENTIAL_KEK_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK"
CREDENTIAL_KEK_PREVIOUS_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS"
EVENT_SIGNING_KEY_AAD = "github_integration.event_signing_key"


def credential_keks() -> tuple[bytes, ...]:
    """Return the rotatable KEKs that seal provider credentials at rest."""
    from .sealed_secret import SealedSecretError, load_keks_from_env

    try:
        return load_keks_from_env(
            CREDENTIAL_KEK_VARIABLE,
            previous_variable=CREDENTIAL_KEK_PREVIOUS_VARIABLE,
        )
    except SealedSecretError as error:
        raise HostedConfigurationError(str(error)) from error


def credential_kek() -> bytes:
    """Return the current KEK used for new sealed credential writes."""
    return credential_keks()[0]


@dataclass(frozen=True)
class OAuthConfiguration:
    client_id: str
    client_secret: str


def oauth_configuration() -> OAuthConfiguration:
    return OAuthConfiguration(
        client_id=required("GITHUB_OAUTH_CLIENT_ID"),
        client_secret=required("GITHUB_OAUTH_CLIENT_SECRET"),
    )
