"""GitHub App installation-token authentication.

Diffuse previously authenticated to GitHub with one static ``GITHUB_TOKEN``,
which the environment files described as "a GitHub App installation token or
user access token". Only the second half of that was workable: an installation
token expires after one hour, and nothing here renewed it, so the option that
sounded correct silently stopped working mid-afternoon and the option that
worked was a personal access token carrying a human's identity.

This module closes that gap. Given an App id, an installation id, and the App's
private key, it mints a short-lived RS256 JWT, exchanges it for an installation
token, and caches that token until shortly before it expires.

``github_token()`` is the single resolver every GitHub call site uses. When App
credentials are configured it returns a live installation token; otherwise it
falls back to ``GITHUB_TOKEN``, so a deployment that has not migrated keeps
working unchanged.

The exchange is synchronous, and some callers are async request handlers. That
is a deliberate trade: the call happens roughly once an hour per process rather
than per request, and threading an async path through nine synchronous
``_headers()`` functions would have been a much larger change for a block that
is measured in milliseconds an hour. If that ever shows up in latency, the fix
is to refresh from the worker's existing scheduler rather than lazily here.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import httpx
import jwt

from diffuse.repository.scm import normalize_base_url, scm_api_timeout_seconds

LOGGER = logging.getLogger(__name__)

APP_ID_VARIABLE = "GITHUB_APP_ID"
INSTALLATION_ID_VARIABLE = "GITHUB_APP_INSTALLATION_ID"
PRIVATE_KEY_FILE_VARIABLE = "GITHUB_APP_PRIVATE_KEY_FILE"
PRIVATE_KEY_VARIABLE = "GITHUB_APP_PRIVATE_KEY"
GITHUB_INTEGRATION_URL_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_URL"
GITHUB_INTEGRATION_TOKEN_VARIABLE = "DIFFUSE_GITHUB_INTEGRATION_TOKEN"
DEFAULT_GITHUB_API_VERSION = "2026-03-10"

MAX_PRIVATE_KEY_BYTES = 16_384
MAX_TOKEN_RESPONSE_BYTES = 64_000

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

    Subclasses ValueError because that is the contract
    ``diffuse.worker`` classifies as non-retryable: a malformed private
    key is not going to become well-formed on the fourth attempt, and retrying
    re-clones the repository.
    """


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    installation_id: str
    private_key: str


_cache_lock = threading.Lock()
_cached_token: str | None = None
_cached_expires_at: float = 0.0
# Credentials are resolved per exchange rather than cached, so rotating the key
# file takes effect on the next refresh. This records what the cached token was
# minted from, so rotating to a *different* installation invalidates it rather
# than serving a token for the previous one until it expires.
_cached_identity: tuple[str, str] | None = None


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


def app_credentials() -> AppCredentials | None:
    """Resolve App credentials, or None when App authentication is not in use.

    Returns None only when *nothing* is configured. A partial configuration
    raises instead: silently falling back to ``GITHUB_TOKEN`` because one of
    three variables was missing is how a deployment ends up authenticating as
    something other than what its operator believes.
    """
    app_id = os.environ.get(APP_ID_VARIABLE, "").strip()
    installation_id = os.environ.get(INSTALLATION_ID_VARIABLE, "").strip()
    private_key = _private_key()
    if not any((app_id, installation_id, private_key)):
        return None

    missing = [
        name
        for name, value in (
            (APP_ID_VARIABLE, app_id),
            (INSTALLATION_ID_VARIABLE, installation_id),
            (f"{PRIVATE_KEY_FILE_VARIABLE} or {PRIVATE_KEY_VARIABLE}", private_key),
        )
        if not value
    ]
    if missing:
        raise GitHubAppConfigurationError(
            "GitHub App authentication is partially configured; missing "
            f"{', '.join(missing)}. Set all three, or unset them all to "
            "authenticate with GITHUB_TOKEN instead."
        )
    if not APP_ID_PATTERN.fullmatch(app_id):
        raise GitHubAppConfigurationError(
            f"{APP_ID_VARIABLE} must be the App's numeric id or its client id"
        )
    if not INSTALLATION_ID_PATTERN.fullmatch(installation_id):
        raise GitHubAppConfigurationError(
            f"{INSTALLATION_ID_VARIABLE} must be the numeric installation id. "
            "It is the trailing number in the installation's settings URL, not "
            "the App id."
        )
    if "PRIVATE KEY" not in private_key:
        raise GitHubAppConfigurationError(
            "The GitHub App private key is not a PEM document. Download the "
            "`.pem` GitHub generates and provide its contents unmodified."
        )
    return AppCredentials(app_id, installation_id, private_key)


@dataclass(frozen=True)
class GitHubIntegrationTokenBroker:
    """The narrow credential bridge for the Diffuse GitHub App.

    The customer-operated instance proves only its own connection credential to
    this endpoint. It never receives the shared App private key.
    """

    url: str
    instance_token: str


def github_integration_token_broker() -> GitHubIntegrationTokenBroker | None:
    url = os.environ.get(GITHUB_INTEGRATION_URL_VARIABLE, "").strip().rstrip("/")
    instance_token = os.environ.get(GITHUB_INTEGRATION_TOKEN_VARIABLE, "").strip()
    if not url and not instance_token:
        return None
    if not url or not instance_token:
        raise GitHubAppConfigurationError(
            "GitHub Integration Service authentication is partially configured; set both "
            f"{GITHUB_INTEGRATION_URL_VARIABLE} and {GITHUB_INTEGRATION_TOKEN_VARIABLE}."
        )
    parsed = normalize_base_url(url, field_name=GITHUB_INTEGRATION_URL_VARIABLE)
    if not parsed.startswith("https://"):
        raise GitHubAppConfigurationError(
            f"{GITHUB_INTEGRATION_URL_VARIABLE} must be an HTTPS origin"
        )
    if len(instance_token) < 32:
        raise GitHubAppConfigurationError(
            f"{GITHUB_INTEGRATION_TOKEN_VARIABLE} is too short"
        )
    return GitHubIntegrationTokenBroker(parsed, instance_token)


def _mint_app_jwt(credentials: AppCredentials) -> str:
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
    except Exception as error:
        # PyJWT surfaces a malformed or non-RSA key as a bare ValueError whose
        # message quotes the key material, which must not reach a log.
        raise GitHubAppConfigurationError(
            "The GitHub App private key could not sign a token. It must be the "
            "unmodified RSA PEM GitHub issued for this App."
        ) from error


def validate_app_configuration() -> None:
    """Parse configured credentials and prove the private key can sign.

    Token exchange stays lazy because it requires GitHub to be reachable, but a
    malformed PEM is deterministic configuration. Catch it at worker startup
    rather than dead-lettering the first claimed review job.
    """
    broker = github_integration_token_broker()
    credentials = app_credentials()
    if broker is not None and credentials is not None:
        raise GitHubAppConfigurationError(
            "Configure either local GitHub App credentials or GitHub Integration Service "
            "credentials, not both."
        )
    if credentials is not None:
        _mint_app_jwt(credentials)


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


def installation_token(credentials: AppCredentials | None = None) -> str:
    """Return a live installation token, minting one when the cache is cold."""
    global _cached_token, _cached_expires_at, _cached_identity

    credentials = credentials or app_credentials()
    if credentials is None:
        raise GitHubAppConfigurationError(
            "GitHub App authentication is not configured"
        )
    identity = (credentials.app_id, credentials.installation_id)

    with _cache_lock:
        fresh = (
            _cached_token is not None
            and _cached_identity == identity
            and time.monotonic() < _cached_expires_at - TOKEN_REFRESH_MARGIN_SECONDS
        )
        if fresh:
            return _cached_token

        token, expires_at = _exchange_for_installation_token(credentials)
        _cached_token = token
        _cached_expires_at = expires_at
        _cached_identity = identity
        LOGGER.info(
            "Minted a GitHub App installation token for installation %s",
            credentials.installation_id,
        )
        return token


def integration_installation_token(broker: GitHubIntegrationTokenBroker | None = None) -> str:
    """Get a cached installation token from the GitHub Integration Service."""
    global _cached_token, _cached_expires_at, _cached_identity

    broker = broker or github_integration_token_broker()
    if broker is None:
        raise GitHubAppConfigurationError(
            "GitHub Integration Service authentication is not configured"
        )
    identity = (
        "hosted",
        f"{broker.url}:{sha256(broker.instance_token.encode()).hexdigest()}",
    )
    with _cache_lock:
        fresh = (
            _cached_token is not None
            and _cached_identity == identity
            and time.monotonic() < _cached_expires_at - TOKEN_REFRESH_MARGIN_SECONDS
        )
        if fresh:
            return _cached_token
        try:
            response = httpx.post(
                f"{broker.url}/v1/installation-token",
                headers={"Authorization": f"Bearer {broker.instance_token}"},
                timeout=scm_api_timeout_seconds(),
            )
        except httpx.HTTPError as error:
            raise GitHubAppError(
                f"Could not reach the GitHub Integration Service: {error}"
            ) from error
        if response.status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            raise GitHubAppConfigurationError(
                "GitHub Integration Service rejected this self-hosted "
                "instance credential; reconnect it."
            )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise GitHubAppError(
                "GitHub Integration Service could not mint an installation token "
                f"(HTTP {response.status_code})"
            )
        try:
            token = response.json()["token"]
        except (ValueError, KeyError, TypeError) as error:
            raise GitHubAppError(
                "GitHub Integration Service returned an invalid installation token"
            ) from error
        if not isinstance(token, str) or not token:
            raise GitHubAppError("GitHub Integration Service returned an empty installation token")
        _cached_token = token
        _cached_expires_at = time.monotonic() + 3600.0
        _cached_identity = identity
        return token


def reset_installation_token_cache() -> None:
    """Drop the cached token. For tests, and for a forced refresh after a 401."""
    global _cached_token, _cached_expires_at, _cached_identity
    with _cache_lock:
        _cached_token = None
        _cached_expires_at = 0.0
        _cached_identity = None


def github_token() -> str:
    """The credential every GitHub call site authenticates with.

    Prefers a GitHub App installation token, because it is scoped to the App's
    permissions and installation rather than to a person, and expires on its
    own. Falls back to ``GITHUB_TOKEN`` when no App is configured.
    """
    broker = github_integration_token_broker()
    if broker is not None:
        return integration_installation_token(broker)
    credentials = app_credentials()
    if credentials is not None:
        return installation_token(credentials)
    return os.environ.get("GITHUB_TOKEN", "")
