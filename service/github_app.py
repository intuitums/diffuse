"""GitHub App installation-token authentication.

Diffuse previously authenticated to GitHub with one static ``GITHUB_TOKEN``,
which the environment files described as "a GitHub App installation token or
user access token". Only the second half of that was workable: an installation
token expires after one hour, and nothing here renewed it, so the option that
sounded correct silently stopped working mid-afternoon and the option that
worked was a personal access token carrying a human's identity.

This module closes that gap in both supported deployment modes. A standalone
node uses an App id, installation id, and private key locally. A relay node
exchanges its node credential with the hosted integration gateway, where the
App identity remains. Both paths cache the returned installation token until
shortly before it expires.

``github_token()`` is the single resolver every GitHub call site uses. It
prefers relay credentials, then standalone App credentials, and finally the
compatibility-only ``GITHUB_TOKEN`` fallback.

The exchange is synchronous, and some callers are async request handlers. That
is a deliberate trade: the call happens roughly once an hour per process rather
than per request, and threading an async path through nine synchronous
``_headers()`` functions would have been a much larger change for a block that
is measured in milliseconds an hour. If that ever shows up in latency, the fix
is to refresh from the worker's existing scheduler rather than lazily here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

from service.scm import normalize_base_url, scm_api_timeout_seconds

LOGGER = logging.getLogger(__name__)

APP_ID_VARIABLE = "GITHUB_APP_ID"
INSTALLATION_ID_VARIABLE = "GITHUB_APP_INSTALLATION_ID"
PRIVATE_KEY_FILE_VARIABLE = "GITHUB_APP_PRIVATE_KEY_FILE"
PRIVATE_KEY_VARIABLE = "GITHUB_APP_PRIVATE_KEY"
RELAY_URL_VARIABLE = "DIFFUSE_RELAY_URL"
RELAY_TOKEN_VARIABLE = "DIFFUSE_RELAY_TOKEN"
DEFAULT_GITHUB_API_VERSION = "2026-03-10"

MAX_PRIVATE_KEY_BYTES = 16_384
MAX_TOKEN_RESPONSE_BYTES = 64_000
MAX_INSTALLATION_RESPONSE_BYTES = 256_000

# GitHub accepts either the numeric App id or the App's client id as `iss`.
APP_ID_PATTERN = re.compile(r"[0-9]{1,20}|Iv[0-9A-Za-z._-]{6,253}")
INSTALLATION_ID_PATTERN = re.compile(r"[0-9]{1,20}")

# GitHub rejects an App JWT whose lifetime exceeds ten minutes. Nine leaves room
# for the backdating below without approaching the limit.
JWT_LIFETIME_SECONDS = 540
# GitHub rejects a JWT whose `iat` is in the future by its clock. Backdating
# absorbs drift between this host and GitHub.
JWT_BACKDATE_SECONDS = 60
# Installation tokens last an hour. Refreshing early means a request never
# carries a token that expires while it is in flight.
TOKEN_REFRESH_MARGIN_SECONDS = 300


class GitHubAppError(Exception):
    """The GitHub App credentials could not be used."""


class GitHubAppConfigurationError(GitHubAppError, ValueError):
    """The configured App credentials are absent or malformed.

    Subclasses ValueError because that is the contract ``service.worker``
    classifies as non-retryable: a malformed private key is not going to become
    well-formed on the fourth attempt, and retrying re-clones the repository.
    """


@dataclass(frozen=True)
class AppIdentity:
    app_id: str
    private_key: str


@dataclass(frozen=True)
class AppCredentials(AppIdentity):
    installation_id: str


@dataclass(frozen=True)
class RelayCredentials:
    base_url: str
    token: str


@dataclass(frozen=True)
class GitHubInstallation:
    id: int
    account_login: str


_cache_lock = threading.Lock()
# The gateway serves many installations, while a node serves one. A bounded
# dictionary avoids serially evicting every installation token on the gateway.
_cached_tokens: dict[tuple[str, str], tuple[str, float]] = {}
MAX_CACHED_INSTALLATION_TOKENS = 10_000


def _api_url() -> str:
    return normalize_base_url(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        field_name="GITHUB_API_URL",
    )


def _read_private_key_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as error:
        raise GitHubAppConfigurationError(
            f"GitHub App private key file {path} is unreadable"
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise GitHubAppConfigurationError(
            f"GitHub App private key file {path} is not a regular file"
        )
    if info.st_size > MAX_PRIVATE_KEY_BYTES:
        raise GitHubAppConfigurationError(
            f"GitHub App private key file {path} is implausibly large"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GitHubAppConfigurationError(
            f"GitHub App private key file {path} is group- or world-writable"
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        LOGGER.warning(
            "GitHub App private key file %s is group- or world-readable; "
            "mode 600 is expected",
            path,
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GitHubAppConfigurationError(
            f"GitHub App private key file {path} is unreadable"
        ) from error


def _private_key() -> str | None:
    """Resolve the App private key, preferring the file-backed form.

    The file is preferred because it can be mounted read-only at mode 600 and
    rotated without recreating the container, and because a PEM in the process
    environment is readable by anything that can read ``/proc/self/environ``.
    The inline variable exists because several secret managers only inject
    environment variables, and a slightly weaker delivery mechanism is better
    than an operator giving up and committing the key.
    """
    configured_path = os.environ.get(PRIVATE_KEY_FILE_VARIABLE, "").strip()
    if configured_path:
        return _read_private_key_file(Path(configured_path))
    inline = os.environ.get(PRIVATE_KEY_VARIABLE, "")
    if len(inline.encode("utf-8")) > MAX_PRIVATE_KEY_BYTES:
        raise GitHubAppConfigurationError(
            f"{PRIVATE_KEY_VARIABLE} is implausibly large"
        )
    # Compose and several secret managers deliver a multi-line PEM with literal
    # backslash-n rather than real newlines; the PEM parser rejects that with an
    # error that says nothing about the cause.
    return inline.replace("\\n", "\n").strip() or None


def app_identity() -> AppIdentity | None:
    """Resolve the long-lived App identity without selecting an installation."""
    app_id = os.environ.get(APP_ID_VARIABLE, "").strip()
    private_key = _private_key()
    if not any((app_id, private_key)):
        return None

    missing = [
        name
        for name, value in (
            (APP_ID_VARIABLE, app_id),
            (f"{PRIVATE_KEY_FILE_VARIABLE} or {PRIVATE_KEY_VARIABLE}", private_key),
        )
        if not value
    ]
    if missing:
        raise GitHubAppConfigurationError(
            "GitHub App authentication is partially configured; missing "
            f"{', '.join(missing)}. Set both identity values, or unset them."
        )
    if not APP_ID_PATTERN.fullmatch(app_id):
        raise GitHubAppConfigurationError(
            f"{APP_ID_VARIABLE} must be the App's numeric id or its client id"
        )
    if "PRIVATE KEY" not in private_key:
        raise GitHubAppConfigurationError(
            "The GitHub App private key is not a PEM document. Download the "
            "`.pem` GitHub generates and provide its contents unmodified."
        )
    return AppIdentity(app_id=app_id, private_key=private_key)


def app_credentials() -> AppCredentials | None:
    """Resolve standalone App credentials, or None when App auth is unused."""
    identity = app_identity()
    installation_id = os.environ.get(INSTALLATION_ID_VARIABLE, "").strip()
    if identity is None and not installation_id:
        return None
    if identity is None or not installation_id:
        missing = (
            f"{APP_ID_VARIABLE} and {PRIVATE_KEY_FILE_VARIABLE} or {PRIVATE_KEY_VARIABLE}"
            if identity is None
            else INSTALLATION_ID_VARIABLE
        )
        raise GitHubAppConfigurationError(
            "GitHub App authentication is partially configured; missing "
            f"{missing}. A hosted relay configures the App identity without an "
            "installation only in the `diffuse gateway` process."
        )
    if not INSTALLATION_ID_PATTERN.fullmatch(installation_id):
        raise GitHubAppConfigurationError(
            f"{INSTALLATION_ID_VARIABLE} must be the numeric installation id. "
            "It is the trailing number in the installation's settings URL, not "
            "the App id."
        )
    return AppCredentials(
        app_id=identity.app_id,
        private_key=identity.private_key,
        installation_id=installation_id,
    )


def app_credentials_for_installation(installation_id: int) -> AppCredentials:
    if (
        isinstance(installation_id, bool)
        or not isinstance(installation_id, int)
        or installation_id <= 0
    ):
        raise GitHubAppConfigurationError("GitHub installation id must be positive")
    identity = app_identity()
    if identity is None:
        raise GitHubAppConfigurationError("GitHub App identity is not configured")
    return AppCredentials(
        app_id=identity.app_id,
        private_key=identity.private_key,
        installation_id=str(installation_id),
    )


def relay_credentials() -> RelayCredentials | None:
    base_url = os.environ.get(RELAY_URL_VARIABLE, "").strip()
    token = os.environ.get(RELAY_TOKEN_VARIABLE, "").strip()
    if not any((base_url, token)):
        return None
    if not base_url or not token:
        missing = RELAY_URL_VARIABLE if not base_url else RELAY_TOKEN_VARIABLE
        raise GitHubAppConfigurationError(
            f"Diffuse relay authentication is partially configured; missing {missing}"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
        raise GitHubAppConfigurationError(f"{RELAY_TOKEN_VARIABLE} is malformed")
    return RelayCredentials(
        base_url=normalize_base_url(base_url, field_name=RELAY_URL_VARIABLE),
        token=token,
    )


def _mint_app_jwt(credentials: AppIdentity) -> str:
    issued_at = int(time.time()) - JWT_BACKDATE_SECONDS
    try:
        return jwt.encode(
            {
                "iat": issued_at,
                "exp": issued_at + JWT_LIFETIME_SECONDS,
                "iss": credentials.app_id,
            },
            credentials.private_key,
            algorithm="RS256",
        )
    except Exception:
        # PyJWT surfaces a malformed or non-RSA key as a bare ValueError whose
        # message quotes the key material, which must not reach a log.
        raise GitHubAppConfigurationError(
            "The GitHub App private key could not sign a token. It must be the "
            "unmodified RSA PEM GitHub issued for this App."
        ) from None


def validate_app_configuration() -> None:
    """Parse configured credentials and prove the private key can sign.

    Token exchange stays lazy because it requires GitHub to be reachable, but a
    malformed PEM is deterministic configuration. Catch it at worker startup
    rather than dead-lettering the first claimed review job.
    """
    relay = relay_credentials()
    if relay is not None:
        if any(
            os.environ.get(name, "").strip()
            for name in (
                APP_ID_VARIABLE,
                INSTALLATION_ID_VARIABLE,
                PRIVATE_KEY_FILE_VARIABLE,
                PRIVATE_KEY_VARIABLE,
                "GITHUB_TOKEN",
            )
        ):
            raise GitHubAppConfigurationError(
                "A Diffuse node using DIFFUSE_RELAY_URL must not also configure "
                "GitHub App credentials or GITHUB_TOKEN"
            )
        return
    credentials = app_credentials()
    if credentials is not None:
        _mint_app_jwt(credentials)


def validate_gateway_app_configuration() -> None:
    identity = app_identity()
    if identity is None:
        raise GitHubAppConfigurationError(
            "The integration gateway requires GITHUB_APP_ID and a private key"
        )
    if os.environ.get(INSTALLATION_ID_VARIABLE, "").strip():
        raise GitHubAppConfigurationError(
            "The integration gateway selects installations per node; "
            f"{INSTALLATION_ID_VARIABLE} must be unset"
        )
    _mint_app_jwt(identity)


def _exchange_for_installation_token(credentials: AppCredentials) -> tuple[str, float]:
    url = (
        f"{_api_url()}/app/installations/"
        f"{credentials.installation_id}/access_tokens"
    )
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {_mint_app_jwt(credentials)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": os.environ.get(
                    "GITHUB_API_VERSION",
                    DEFAULT_GITHUB_API_VERSION,
                ),
                "User-Agent": "diffuse-github-app",
            },
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        # Transient by assumption: the worker should retry rather than
        # dead-letter a review because GitHub was briefly unreachable.
        raise GitHubAppError(
            f"Could not reach GitHub to mint an installation token: {error}"
        ) from error

    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise GitHubAppConfigurationError(
            "GitHub rejected the App JWT. Check that GITHUB_APP_ID matches the "
            "private key, and that the host clock is accurate."
        )
    if response.status_code == httpx.codes.NOT_FOUND:
        raise GitHubAppConfigurationError(
            f"GitHub has no installation {credentials.installation_id} for this "
            "App. Check GITHUB_APP_INSTALLATION_ID."
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise GitHubAppError(
            "GitHub refused to mint an installation token "
            f"(HTTP {response.status_code})"
        )

    if len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
        raise GitHubAppError("GitHub returned an implausibly large token response")
    try:
        payload = response.json()
        token = payload["token"]
    except (ValueError, KeyError, TypeError) as error:
        raise GitHubAppError(
            "GitHub returned a token response Diffuse could not read"
        ) from error
    if not isinstance(token, str) or not token:
        raise GitHubAppError("GitHub returned an empty installation token")

    # Trust our own clock over the returned timestamp: `expires_at` is GitHub's
    # wall clock, and comparing it against ours would import any drift between
    # them into the refresh decision. The lifetime is a documented hour.
    return token, time.monotonic() + 3600.0


def verify_installation(installation_id: int) -> GitHubInstallation:
    """Verify an installation id against GitHub using the relay's App identity."""
    credentials = app_credentials_for_installation(installation_id)
    try:
        response = httpx.get(
            f"{_api_url()}/app/installations/{credentials.installation_id}",
            headers={
                "Authorization": f"Bearer {_mint_app_jwt(credentials)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": os.environ.get(
                    "GITHUB_API_VERSION",
                    DEFAULT_GITHUB_API_VERSION,
                ),
                "User-Agent": "diffuse-integration-relay",
            },
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise GitHubAppError("Could not reach GitHub to verify the installation") from error
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise GitHubAppConfigurationError(
            "GitHub rejected the App JWT while verifying an installation"
        )
    if response.status_code == httpx.codes.NOT_FOUND:
        raise GitHubAppError("GitHub App installation could not be verified")
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise GitHubAppError(
            f"GitHub refused installation verification (HTTP {response.status_code})"
        )
    if len(response.content) > MAX_INSTALLATION_RESPONSE_BYTES:
        raise GitHubAppError("GitHub returned an implausibly large installation response")
    try:
        payload = response.json()
        returned_id = payload["id"]
        account_login = payload["account"]["login"]
    except (ValueError, KeyError, TypeError) as error:
        raise GitHubAppError(
            "GitHub returned an installation response Diffuse could not read"
        ) from error
    if (
        isinstance(returned_id, bool)
        or not isinstance(returned_id, int)
        or returned_id != installation_id
        or not isinstance(account_login, str)
        or not 1 <= len(account_login) <= 255
        or "\x00" in account_login
    ):
        raise GitHubAppError("GitHub returned invalid installation identity")
    return GitHubInstallation(id=returned_id, account_login=account_login)


def _exchange_relay_token(credentials: RelayCredentials) -> tuple[str, float]:
    try:
        response = httpx.post(
            f"{credentials.base_url}/relay/v1/github/token",
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
                "User-Agent": "diffuse-node",
            },
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise GitHubAppError("Could not reach the Diffuse integration relay") from error
    if response.status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        raise GitHubAppConfigurationError(
            "The Diffuse integration relay rejected this node credential"
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise GitHubAppError(
            f"Diffuse integration relay refused a GitHub token (HTTP {response.status_code})"
        )
    if len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
        raise GitHubAppError("Diffuse relay returned an implausibly large token response")
    try:
        payload = response.json()
        token = payload["token"]
        expires_in = payload["expiresIn"]
    except (ValueError, KeyError, TypeError) as error:
        raise GitHubAppError(
            "Diffuse relay returned a token response this node could not read"
        ) from error
    if (
        not isinstance(token, str)
        or not token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 300 <= expires_in <= 3600
    ):
        raise GitHubAppError("Diffuse relay returned invalid GitHub token metadata")
    return token, time.monotonic() + expires_in


def _cached_or_exchange(
    identity: tuple[str, str],
    exchange,
) -> str:
    with _cache_lock:
        now = time.monotonic()
        cached = _cached_tokens.get(identity)
        if cached is not None and now < cached[1] - TOKEN_REFRESH_MARGIN_SECONDS:
            return cached[0]
        token, expires_at = exchange()
        if len(_cached_tokens) >= MAX_CACHED_INSTALLATION_TOKENS:
            expired = [
                key
                for key, (_token, expiry) in _cached_tokens.items()
                if now >= expiry - TOKEN_REFRESH_MARGIN_SECONDS
            ]
            for key in expired:
                _cached_tokens.pop(key, None)
            if len(_cached_tokens) >= MAX_CACHED_INSTALLATION_TOKENS:
                _cached_tokens.pop(next(iter(_cached_tokens)))
        _cached_tokens[identity] = (token, expires_at)
        return token


def installation_token(credentials: AppCredentials | None = None) -> str:
    """Return a live installation token, minting one when the cache is cold."""
    credentials = credentials or app_credentials()
    if credentials is None:
        raise GitHubAppConfigurationError(
            "GitHub App authentication is not configured"
        )
    identity = (credentials.app_id, credentials.installation_id)
    token = _cached_or_exchange(
        identity,
        lambda: _exchange_for_installation_token(credentials),
    )
    LOGGER.debug(
        "Resolved a GitHub App installation token for installation %s",
        credentials.installation_id,
    )
    return token


def mint_installation_token_for_installation(installation_id: int) -> tuple[str, int]:
    """Mint a fresh token for a paired node.

    A fresh exchange lets the relay state the full lifetime accurately. Returning
    a cached gateway token with a new one-hour lifetime would let the node use it
    after GitHub had already expired it.
    """
    credentials = app_credentials_for_installation(installation_id)
    token, _expires_at = _exchange_for_installation_token(credentials)
    return token, 3600


def relay_installation_token(credentials: RelayCredentials | None = None) -> str:
    credentials = credentials or relay_credentials()
    if credentials is None:
        raise GitHubAppConfigurationError("Diffuse relay authentication is not configured")
    identity = (
        f"relay:{credentials.base_url}",
        hashlib.sha256(credentials.token.encode()).hexdigest(),
    )
    return _cached_or_exchange(
        identity,
        lambda: _exchange_relay_token(credentials),
    )


def reset_installation_token_cache() -> None:
    """Drop the cached token. For tests, and for a forced refresh after a 401."""
    with _cache_lock:
        _cached_tokens.clear()


def github_token() -> str:
    """The credential every GitHub call site authenticates with.

    Prefers a GitHub App installation token, because it is scoped to the App's
    permissions and installation rather than to a person, and expires on its
    own. Falls back to ``GITHUB_TOKEN`` when no App is configured.
    """
    relay = relay_credentials()
    if relay is not None:
        return relay_installation_token(relay)
    credentials = app_credentials()
    if credentials is not None:
        return installation_token(credentials)
    return os.environ.get("GITHUB_TOKEN", "")
