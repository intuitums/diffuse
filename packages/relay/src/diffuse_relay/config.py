"""Fail-closed configuration for the GitHub Integration Service."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_INSTALLATION_ID = re.compile(r"^[0-9]{1,20}$")

# Mirrors LOOPBACK_HOSTS in diffuse/repository/scm.py: the same three parsed-hostname
# forms. The relay cannot import the server package, so the set is copied; if a
# fourth loopback spelling ever matters, change both.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class IntegrationConfigurationError(ValueError):
    """The integration service cannot safely start with its current configuration."""


# Back-compat alias for any external imports.
HostedConfigurationError = IntegrationConfigurationError


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise IntegrationConfigurationError(f"{name} must be configured")
    return value


def optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def public_url() -> str:
    """The relay's browser-reachable origin.

    Production requires an HTTPS origin. Plain http is accepted only for
    loopback hosts with no path, query, or fragment, so a locally running
    relay can drive the connect flow end to end.
    """
    value = required("DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc and "/" not in value[len("https://") :]:
        return value
    if (
        parsed.scheme == "http"
        and parsed.hostname in _LOOPBACK_HOSTS
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ):
        return value
    raise IntegrationConfigurationError(
        "DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL must be an HTTPS origin; "
        "http is accepted only for localhost, 127.0.0.1, or ::1 (local testing)"
    )


def github_app_id() -> str:
    value = required("GITHUB_APP_ID")
    if not _INSTALLATION_ID.fullmatch(value):
        raise IntegrationConfigurationError("GITHUB_APP_ID must be numeric")
    return value


def github_private_key() -> str:
    value = required("GITHUB_APP_PRIVATE_KEY")
    if "BEGIN" not in value or "PRIVATE KEY" not in value:
        raise IntegrationConfigurationError("GITHUB_APP_PRIVATE_KEY must be a PEM private key")
    return value.replace("\\n", "\n")


def webhook_secret() -> str:
    return required("GITHUB_WEBHOOK_SECRET")


def database_url() -> str:
    # The Vercel Marketplace Neon integration manages DATABASE_URL for its
    # attached project. Keep the explicit name as an override for operators who
    # intentionally use another PostgreSQL provider, but do not force them to
    # copy a managed connection string into a second secret store.
    return optional("DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL") or required("DATABASE_URL")


def token_key() -> bytes:
    """Return the key that hashes instance credentials in the Relay database."""
    value = required("DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise IntegrationConfigurationError(
            "DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER must be base64url text"
        ) from error
    if len(decoded) < 32:
        raise IntegrationConfigurationError(
            "DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER must decode to 32 bytes"
        )
    return decoded


CREDENTIAL_KEK_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK"
CREDENTIAL_KEK_PREVIOUS_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS"
# Wire AAD strings are stable; do not rename the string values or existing ciphertext
# fails authentication. Python names follow delivery_signing_key vocabulary.
DELIVERY_SIGNING_KEY_AAD = "github_integration.event_signing_key"
CONNECT_INSTANCE_TOKEN_AAD = "github_integration.connect_instance_token"
CONNECT_DELIVERY_SIGNING_KEY_AAD = "github_integration.connect_event_signing_key"
EVENT_SIGNING_KEY_AAD = DELIVERY_SIGNING_KEY_AAD  # back-compat alias
CONNECT_EVENT_SIGNING_KEY_AAD = CONNECT_DELIVERY_SIGNING_KEY_AAD  # back-compat alias
DEFAULT_GITHUB_APP_SLUG = "diffuse-agent"


def github_app_slug() -> str:
    """Public slug used for the install URL (github.com/apps/<slug>)."""
    return optional("GITHUB_APP_SLUG") or DEFAULT_GITHUB_APP_SLUG


def github_app_install_url() -> str:
    return f"https://github.com/apps/{github_app_slug()}/installations/new"


def credential_keks() -> tuple[bytes, ...]:
    """Return the rotatable KEKs that seal provider credentials at rest."""
    from .sealed_secret import SealedSecretError, load_keks_from_env

    try:
        return load_keks_from_env(
            CREDENTIAL_KEK_VARIABLE,
            previous_variable=CREDENTIAL_KEK_PREVIOUS_VARIABLE,
        )
    except SealedSecretError as error:
        raise IntegrationConfigurationError(str(error)) from error


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
