"""GitHub OAuth exchange for the Diffuse CLI sign-in flow.

Diffuse acts as a confidential client: the client secret is read from a
file-backed deployment secret at call time and never leaves this process. The
GitHub access token is used once to resolve the identity and is then dropped —
nothing here persists it.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from service.github.api import GITHUB_API_VERSION
from service.scm import normalize_base_url, scm_api_timeout_seconds

LOGGER = logging.getLogger(__name__)

DEFAULT_CLIENT_SECRET_PATH = "/srv/diffuse/secrets/app-client-secret"
MAX_CLIENT_SECRET_BYTES = 4096
CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{8,255}")
CLIENT_SECRET_PATTERN = re.compile(r"[!-~]{20,512}")
APP_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?")
GITHUB_CODE_PATTERN = re.compile(r"[A-Za-z0-9._-]{8,512}")
MAX_TOKEN_RESPONSE_BYTES = 64_000


class GitHubOAuthError(Exception):
    """The GitHub authorization could not be completed."""


class GitHubOAuthConfigurationError(GitHubOAuthError):
    """Diffuse is missing the credentials it needs to act as an OAuth client."""


@dataclass(frozen=True)
class GitHubIdentity:
    github_user_id: int
    login: str
    avatar_url: str | None


def github_web_url() -> str:
    return normalize_base_url(
        os.environ.get("GITHUB_WEB_URL", "https://github.com"),
        field_name="GITHUB_WEB_URL",
    )


def github_api_url() -> str:
    return normalize_base_url(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        field_name="GITHUB_API_URL",
    )


def oauth_client_id() -> str:
    value = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip()
    if not CLIENT_ID_PATTERN.fullmatch(value):
        raise GitHubOAuthConfigurationError(
            "GITHUB_OAUTH_CLIENT_ID is not configured with a valid GitHub App "
            "client id"
        )
    return value


def oauth_client_secret() -> str:
    """Read the client secret from its file-backed deployment secret.

    Read at call time rather than cached so rotating the file takes effect
    without a restart. The value is never logged and never returned in an error.
    """
    path = Path(
        os.environ.get(
            "GITHUB_OAUTH_CLIENT_SECRET_FILE",
            DEFAULT_CLIENT_SECRET_PATH,
        ).strip()
        or DEFAULT_CLIENT_SECRET_PATH
    )
    try:
        info = path.stat()
    except OSError as error:
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} is unreadable"
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} is not a regular file"
        )
    if info.st_size > MAX_CLIENT_SECRET_BYTES:
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} is implausibly large"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} is group- or world-writable"
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        LOGGER.warning(
            "GitHub client secret file %s is group- or world-readable; "
            "mode 600 is expected",
            path,
        )
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} is unreadable"
        ) from error
    if not CLIENT_SECRET_PATTERN.fullmatch(secret):
        raise GitHubOAuthConfigurationError(
            f"GitHub client secret file {path} does not contain a usable secret"
        )
    return secret


def build_authorize_url(*, state: str, redirect_uri: str | None = None) -> str:
    """Build the GitHub authorization URL the browser is redirected to."""
    parameters = {"client_id": oauth_client_id(), "state": state}
    if redirect_uri is not None:
        parameters["redirect_uri"] = redirect_uri
    return f"{github_web_url()}/login/oauth/authorize?{urlencode(parameters)}"


def build_app_install_url(*, state: str) -> str | None:
    """Build the GitHub App install URL, or None when no app slug is set."""
    slug = os.environ.get("GITHUB_APP_SLUG", "").strip()
    if not APP_SLUG_PATTERN.fullmatch(slug):
        return None
    query = urlencode({"state": state})
    return f"{github_web_url()}/apps/{slug}/installations/new?{query}"


def validate_authorization_code(value: str) -> str:
    if not GITHUB_CODE_PATTERN.fullmatch(value):
        raise GitHubOAuthError("GitHub authorization code is malformed")
    return value


async def exchange_code_for_token(
    code: str,
    *,
    redirect_uri: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Trade an authorization code for a GitHub user access token."""
    payload = {
        "client_id": oauth_client_id(),
        "client_secret": oauth_client_secret(),
        "code": validate_authorization_code(code),
    }
    if redirect_uri is not None:
        payload["redirect_uri"] = redirect_uri
    url = f"{github_web_url()}/login/oauth/access_token"
    headers = {
        "Accept": "application/json",
        "User-Agent": "diffuse-oauth",
    }

    async def _post(active: httpx.AsyncClient) -> httpx.Response:
        return await active.post(url, data=payload, headers=headers)

    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=scm_api_timeout_seconds()
            ) as owned_client:
                response = await _post(owned_client)
        else:
            response = await _post(client)
    except httpx.HTTPError as error:
        raise GitHubOAuthError("GitHub token exchange failed") from error

    if response.status_code != 200:
        # The body can echo request parameters, so only the status is surfaced.
        raise GitHubOAuthError(
            f"GitHub token exchange returned HTTP {response.status_code}"
        )
    if len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
        raise GitHubOAuthError("GitHub token exchange returned an oversized body")
    try:
        body = response.json()
    except ValueError as error:
        raise GitHubOAuthError("GitHub token exchange returned invalid JSON") from error
    if not isinstance(body, dict):
        raise GitHubOAuthError("GitHub token exchange returned invalid JSON")
    if body.get("error"):
        raise GitHubOAuthError(
            f"GitHub rejected the authorization code: {body['error']}"
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not CLIENT_SECRET_PATTERN.fullmatch(
        access_token
    ):
        raise GitHubOAuthError("GitHub token exchange returned no access token")
    return access_token


async def fetch_authenticated_user(
    access_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> GitHubIdentity:
    """Resolve the identity behind a GitHub user access token."""
    url = f"{github_api_url()}/user"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {access_token}",
        "X-GitHub-Api-Version": os.environ.get(
            "GITHUB_API_VERSION", GITHUB_API_VERSION
        ),
        "User-Agent": "diffuse-oauth",
    }

    async def _get(active: httpx.AsyncClient) -> httpx.Response:
        return await active.get(url, headers=headers)

    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=scm_api_timeout_seconds()
            ) as owned_client:
                response = await _get(owned_client)
        else:
            response = await _get(client)
    except httpx.HTTPError as error:
        raise GitHubOAuthError("GitHub user lookup failed") from error

    if response.status_code != 200:
        raise GitHubOAuthError(
            f"GitHub user lookup returned HTTP {response.status_code}"
        )
    try:
        body = response.json()
    except ValueError as error:
        raise GitHubOAuthError("GitHub user lookup returned invalid JSON") from error
    if not isinstance(body, dict):
        raise GitHubOAuthError("GitHub user lookup returned invalid JSON")

    identifier = body.get("id")
    login = body.get("login")
    avatar_url = body.get("avatar_url")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier <= 0
        or not isinstance(login, str)
        or not login
    ):
        raise GitHubOAuthError("GitHub user lookup returned an unusable identity")
    return GitHubIdentity(
        github_user_id=identifier,
        login=login,
        avatar_url=avatar_url if isinstance(avatar_url, str) else None,
    )
