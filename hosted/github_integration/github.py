"""GitHub OAuth verification and installation-token minting for Diffuse."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt

from .config import (
    github_app_id,
    github_app_slug,
    github_private_key,
    oauth_configuration,
    public_url,
)

GITHUB_API = "https://api.github.com"


class GitHubSetupError(RuntimeError):
    """GitHub could not verify a setup user or mint an installation token."""


@dataclass(frozen=True)
class GitHubInstallationSummary:
    id: int
    account_login: str
    account_type: str


def oauth_authorize_url(state: str) -> str:
    config = oauth_configuration()
    return (
        "https://github.com/login/oauth/authorize"
        f"?client_id={config.client_id}&redirect_uri={public_url()}/auth/github/callback"
        f"&state={state}"
    )


def github_app_install_url(*, state: str | None = None) -> str:
    url = f"https://github.com/apps/{github_app_slug()}/installations/new"
    if state:
        return f"{url}?state={state}"
    return url


def _app_jwt() -> str:
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": github_app_id()},
        github_private_key(),
        algorithm="RS256",
    )


def exchange_oauth_code(code: str) -> tuple[int, str, tuple[GitHubInstallationSummary, ...]]:
    config = oauth_configuration()
    response = httpx.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": f"{public_url()}/auth/github/callback",
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise GitHubSetupError("GitHub refused the authorization code")
    access_token = response.json().get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GitHubSetupError("GitHub did not return a user access token")
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {access_token}"}
    user_response = httpx.get(f"{GITHUB_API}/user", headers=headers, timeout=15)
    installations_response = httpx.get(
        f"{GITHUB_API}/user/installations", headers=headers, timeout=15
    )
    if user_response.status_code != 200 or installations_response.status_code != 200:
        raise GitHubSetupError("GitHub could not verify the authorized user's installations")
    user = user_response.json()
    installations = installations_response.json().get("installations", [])
    try:
        user_id = int(user["id"])
        login = str(user["login"])
        summaries: list[GitHubInstallationSummary] = []
        for item in installations:
            account = item.get("account") or {}
            summaries.append(
                GitHubInstallationSummary(
                    id=int(item["id"]),
                    account_login=str(account.get("login") or f"installation-{item['id']}"),
                    account_type=str(account.get("type") or "Account"),
                )
            )
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubSetupError("GitHub returned an invalid user authorization response") from error
    return user_id, login, tuple(summaries)


def mint_installation_token(installation_id: int) -> tuple[str, str]:
    response = httpx.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_app_jwt()}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise GitHubSetupError("GitHub could not mint an installation token")
    payload = response.json()
    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, str):
        raise GitHubSetupError("GitHub returned an invalid installation token")
    return token, expires_at
