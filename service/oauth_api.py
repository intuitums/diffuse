"""Browser-facing GitHub OAuth endpoints for relay installation and node pairing.

These three routes establish who installed the shared Diffuse GitHub App:

* `/auth/github` creates server-owned state and redirects the browser to GitHub.
* `/auth/github/callback` verifies the state and records the GitHub identity.
* `/setup` links a GitHub App installation to the user who installed it.

This is GitHub installation authentication, not model-provider authentication.
Diffuse does not mint a CLI session or accept OpenAI/Anthropic account tokens.

Unlike `/api/v1`, the caller here is a browser, so failures render HTML rather
than problem+json. Error copy is deliberately vague about *why* a state failed:
unknown, expired, and replayed are indistinguishable to the client.
"""

from __future__ import annotations

import html
import logging
import os
from contextlib import closing
from urllib.parse import urlsplit

import anyio
import psycopg2.errors
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from indexer.store import get_conn
from service.github_app import GitHubAppError, verify_installation
from service.github_oauth import (
    GitHubOAuthConfigurationError,
    GitHubOAuthError,
    build_app_install_url,
    build_authorize_url,
    exchange_code_for_token,
    fetch_authenticated_user,
)
from service.oauth_store import (
    APP_INSTALL_PURPOSE,
    GITHUB_INSTALL_AUTH_PURPOSE,
    consume_oauth_state,
    create_install_auth_state,
    create_install_state,
    generate_state,
    load_oauth_state,
    parse_installation_id,
    record_user_installation,
    upsert_user,
)
from service.relay_store import (
    InstallationOwnershipError,
    authorize_installation_user,
    create_pairing_code,
)
from service.scm import normalize_base_url

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["Browser sign-in"])

# Long enough for any legitimate nonce or authorization code, short enough that
# a hostile query string is rejected before it reaches validation.
MAX_QUERY_PARAMETER_CHARS = 1024

_BROWSER_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} &middot; Diffuse</title>
<style>
  body {{
    background: #0d0f12;
    color: #e7e9ee;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex;
    min-height: 100vh;
    margin: 0;
    align-items: center;
    justify-content: center;
  }}
  main {{
    background: #16191f;
    border: 1px solid #262b34;
    border-radius: 12px;
    padding: 40px;
    max-width: 30rem;
    text-align: center;
  }}
  h1 {{ font-size: 1.4rem; margin: 0 0 12px; }}
  p {{ color: #a4abb8; margin: 0 0 8px; }}
  a {{ color: #7aa2ff; }}
</style>
</head>
<body>
<main>
<h1>{heading}</h1>
{body}
</main>
</body>
</html>
"""


def _page(*, status_code: int, title: str, heading: str, body: str) -> HTMLResponse:
    document = _PAGE_TEMPLATE.format(
        title=html.escape(title),
        heading=html.escape(heading),
        body=body,
    )
    return HTMLResponse(
        content=document,
        status_code=status_code,
        headers=dict(_BROWSER_HEADERS),
    )


def _error_page(*, status_code: int, heading: str, detail: str) -> HTMLResponse:
    return _page(
        status_code=status_code,
        title="Sign-in failed",
        heading=heading,
        body=f"<p>{html.escape(detail)}</p>",
    )


def _unavailable_page() -> HTMLResponse:
    return _error_page(
        status_code=503,
        heading="Sign-in is unavailable",
        detail=(
            "This Diffuse deployment is not configured for GitHub sign-in yet. "
            "Ask an operator to check the server logs."
        ),
    )


def _rejected_state_page() -> HTMLResponse:
    # Unknown, expired, and already-used states all land here on purpose.
    return _error_page(
        status_code=400,
        heading="This sign-in link is no longer valid",
        detail=(
            "The request could not be verified, or it has already been used. "
            "Open the GitHub App connection page again to start over."
        ),
    )


def _bounded(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_QUERY_PARAMETER_CHARS:
        raise ValueError("Query parameter is too long")
    return value


def gateway_public_url() -> str:
    value = normalize_base_url(
        os.environ.get("DIFFUSE_PUBLIC_URL", "").strip(),
        field_name="DIFFUSE_PUBLIC_URL",
    )
    if urlsplit(value).path not in {"", "/"}:
        raise ValueError("DIFFUSE_PUBLIC_URL must be an origin without a path")
    return value.rstrip("/")


async def _in_transaction(callback, /, **kwargs):
    """Run a store call in a worker thread inside one committed transaction."""

    def run():
        with closing(get_conn()) as conn, conn:
            return callback(conn, **kwargs)

    return await anyio.to_thread.run_sync(run)


@router.get(
    "/auth/github",
    summary="Authenticate a GitHub App installer",
    response_class=RedirectResponse,
)
async def start_github_install_auth():
    nonce = generate_state()
    try:
        authorize_url = build_authorize_url(state=nonce)
    except GitHubOAuthConfigurationError:
        LOGGER.exception("GitHub sign-in is not configured")
        return _unavailable_page()

    try:
        await _in_transaction(
            create_install_auth_state,
            state=nonce,
        )
    except psycopg2.errors.UniqueViolation:
        return _rejected_state_page()

    return RedirectResponse(
        authorize_url,
        status_code=302,
        headers=dict(_BROWSER_HEADERS),
    )


@router.get(
    "/auth/github/callback",
    summary="Complete GitHub installer authentication",
    response_class=HTMLResponse,
)
async def complete_github_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error is not None:
        return _error_page(
            status_code=400,
            heading="GitHub did not complete the sign-in",
            detail="Open the GitHub App connection page again to retry.",
        )
    try:
        raw_code = _bounded(code)
        raw_state = _bounded(state)
    except ValueError:
        return _rejected_state_page()
    if not raw_code or not raw_state:
        return _error_page(
            status_code=400,
            heading="That callback is incomplete",
            detail="GitHub did not return an authorization code and state.",
        )

    # Claim the state before spending an exchange on it: a forged or replayed
    # nonce must never reach GitHub with the client secret attached.
    claimed = await _in_transaction(
        consume_oauth_state,
        state=raw_state,
        purpose=GITHUB_INSTALL_AUTH_PURPOSE,
    )
    if claimed is None:
        return _rejected_state_page()

    try:
        access_token = await exchange_code_for_token(raw_code)
        identity = await fetch_authenticated_user(access_token)
    except GitHubOAuthConfigurationError:
        LOGGER.exception("GitHub sign-in is not configured")
        return _unavailable_page()
    except GitHubOAuthError:
        LOGGER.exception("GitHub authorization exchange failed")
        return _error_page(
            status_code=502,
            heading="GitHub could not confirm your identity",
            detail="Open the GitHub App connection page again to retry.",
        )

    # The GitHub token proves installer identity and is deliberately never
    # persisted. The next state is bound to that identity and can be used only
    # for the GitHub App installation callback.
    def _establish_install_auth(conn):
        user = upsert_user(
            conn,
            github_user_id=identity.github_user_id,
            login=identity.login,
            avatar_url=identity.avatar_url,
        )
        install_state = generate_state()
        create_install_state(conn, state=install_state, user_id=user.id)
        return user, install_state

    try:
        user, install_state = await _in_transaction(_establish_install_auth)
    except ValueError:
        LOGGER.exception("GitHub returned an identity Diffuse cannot store")
        return _error_page(
            status_code=502,
            heading="GitHub could not confirm your identity",
            detail="Open the GitHub App connection page again to retry.",
        )

    return _browser_success_page(login=user.login, install_state=install_state)


def _browser_success_page(*, login: str, install_state: str | None) -> HTMLResponse:
    body = [f"<p>Signed in as {html.escape(login)}. You can close this window.</p>"]
    install_url = (
        None if install_state is None else build_app_install_url(state=install_state)
    )
    if install_url is not None:
        body.append(
            f'<p><a href="{html.escape(install_url, quote=True)}">'
            "Connect a repository</a></p>"
        )
    return _page(
        status_code=200,
        title="Signed in",
        heading="You're all set",
        body="\n".join(body),
    )


@router.get(
    "/setup",
    summary="Link a GitHub App installation to the user who installed it",
    response_class=HTMLResponse,
)
async def complete_app_setup(
    installation_id: str | None = None,
    state: str | None = None,
    setup_action: str | None = None,
):
    try:
        raw_installation = _bounded(installation_id)
        raw_state = _bounded(state)
        if not raw_installation:
            raise ValueError("installation_id is required")
        installation = parse_installation_id(raw_installation)
    except ValueError:
        return _error_page(
            status_code=400,
            heading="That install link is invalid",
            detail="GitHub did not return a usable installation id.",
        )
    if not raw_state:
        return _unlinked_install_page()

    pending = await _in_transaction(
        load_oauth_state,
        state=raw_state,
        purpose=APP_INSTALL_PURPOSE,
    )
    if pending is None or pending.user_id is None:
        return _unlinked_install_page()

    try:
        verified = await anyio.to_thread.run_sync(verify_installation, installation)
    except GitHubAppError as error:
        LOGGER.error(
            "Could not verify GitHub App installation %s: %s",
            installation,
            error,
        )
        return _error_page(
            status_code=502,
            heading="GitHub could not confirm this installation",
            detail="Please retry the installation from Diffuse.",
        )

    def _link_and_issue_pairing_code(conn):
        account_login = authorize_installation_user(
            conn,
            user_id=pending.user_id,
            github_installation_id=verified.id,
        )
        claimed = consume_oauth_state(
            conn,
            state=raw_state,
            purpose=APP_INSTALL_PURPOSE,
        )
        if claimed is None or claimed.user_id != pending.user_id:
            return None
        record_user_installation(
            conn,
            user_id=pending.user_id,
            github_installation_id=verified.id,
            account_login=account_login,
        )
        pairing_code = create_pairing_code(
            conn, user_id=pending.user_id, github_installation_id=verified.id
        )
        return account_login, pairing_code

    try:
        linked = await _in_transaction(_link_and_issue_pairing_code)
    except InstallationOwnershipError:
        return _error_page(
            status_code=409,
            heading="The installation is still being confirmed",
            detail=(
                "Diffuse has not received GitHub's signed installation event yet. "
                "Wait a moment and retry the installation from Diffuse."
            ),
        )
    if linked is None:
        return _unlinked_install_page()
    account_login, pairing_code = linked
    try:
        gateway_url = gateway_public_url()
    except ValueError:
        LOGGER.exception("DIFFUSE_PUBLIC_URL is not configured for node pairing")
        return _unavailable_page()
    escaped_code = html.escape(pairing_code)
    escaped_gateway = html.escape(gateway_url)
    return _page(
        status_code=200,
        title="Installed",
        heading="Installed",
        body=(
            f"<p>Diffuse is connected to {html.escape(account_login)}.</p>"
            "<p>Pair your self-hosted node within ten minutes:</p>"
            "<p><code>diffuse relay pair "
            f"--gateway {escaped_gateway} --code {escaped_code}</code></p>"
            "<p>The pairing code is single-use. Repository code remains on your node.</p>"
        ),
    )


def _unlinked_install_page() -> HTMLResponse:
    # Failing closed matters here: guessing an owner for an unattributed
    # installation would hand one user's repositories to another.
    return _error_page(
        status_code=400,
        heading="That installation could not be linked",
        detail=(
            "Start from Diffuse's GitHub App connection page so the "
            "installation can be attributed to your account."
        ),
    )
