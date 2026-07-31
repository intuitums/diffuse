"""Browser-facing OAuth endpoints for the Diffuse CLI sign-in flow.

These three routes are the backend half of `diffuse login`:

* `/auth/cli` records where the CLI is listening and bounces to GitHub.
* `/auth/github/callback` verifies the state, exchanges the code, and hands the
  session token back to the CLI over loopback.
* `/setup` links a GitHub App installation to the user who installed it.

Unlike `/api/v1`, the caller here is a browser, so failures render HTML rather
than problem+json. Error copy is deliberately vague about *why* a state failed:
unknown, expired, and replayed are indistinguishable to the client.
"""

from __future__ import annotations

import html
import logging
from contextlib import closing

import anyio
import psycopg2.errors
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from indexer.store import get_conn
from service.hosted.github_oauth import (
    GitHubOAuthConfigurationError,
    GitHubOAuthError,
    build_app_install_url,
    build_authorize_url,
    exchange_code_for_token,
    fetch_authenticated_user,
)
from service.hosted.oauth_store import (
    APP_INSTALL_PURPOSE,
    CLI_LOGIN_PURPOSE,
    build_loopback_redirect,
    consume_oauth_state,
    create_install_state,
    create_login_state,
    create_session,
    generate_session_token,
    generate_state,
    parse_callback_port,
    parse_installation_id,
    upsert_user,
    validate_state,
)

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
            "Run `diffuse login` again to start over."
        ),
    )


def _bounded(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_QUERY_PARAMETER_CHARS:
        raise ValueError("Query parameter is too long")
    return value


async def _in_transaction(callback, /, **kwargs):
    """Run a store call in a worker thread inside one committed transaction."""

    def run():
        with closing(get_conn()) as conn, conn:
            return callback(conn, **kwargs)

    return await anyio.to_thread.run_sync(run)


@router.get(
    "/auth/cli",
    summary="Start a CLI sign-in and redirect to GitHub",
    response_class=RedirectResponse,
)
async def start_cli_login(port: str | None = None, state: str | None = None):
    try:
        raw_port = _bounded(port)
        raw_state = _bounded(state)
        callback_port = None if raw_port is None else parse_callback_port(raw_port)
        # A browser-initiated sign-in supplies no nonce, so Diffuse mints one.
        nonce = generate_state() if raw_state is None else validate_state(raw_state)
    except ValueError as error:
        return _error_page(
            status_code=400,
            heading="That sign-in request is invalid",
            detail=str(error),
        )

    try:
        authorize_url = build_authorize_url(state=nonce)
    except GitHubOAuthConfigurationError:
        LOGGER.exception("GitHub sign-in is not configured")
        return _unavailable_page()

    try:
        await _in_transaction(
            create_login_state,
            state=nonce,
            callback_port=callback_port,
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
    summary="Complete a GitHub authorization and hand the CLI its token",
    response_class=RedirectResponse,
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
            detail="You can close this window and run `diffuse login` again.",
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
        purpose=CLI_LOGIN_PURPOSE,
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
            detail="Close this window and run `diffuse login` again.",
        )

    # The GitHub token proved the identity and is deliberately never persisted.
    session_token = generate_session_token()

    def _establish_session(conn):
        user = upsert_user(
            conn,
            github_user_id=identity.github_user_id,
            login=identity.login,
            avatar_url=identity.avatar_url,
        )
        create_session(conn, user_id=user.id, token=session_token)
        install_state = None
        if claimed.callback_port is None:
            install_state = generate_state()
            create_install_state(conn, state=install_state, user_id=user.id)
        return user, install_state

    try:
        user, install_state = await _in_transaction(_establish_session)
    except ValueError:
        LOGGER.exception("GitHub returned an identity Diffuse cannot store")
        return _error_page(
            status_code=502,
            heading="GitHub could not confirm your identity",
            detail="Close this window and run `diffuse login` again.",
        )

    if claimed.callback_port is None:
        return _browser_success_page(login=user.login, install_state=install_state)

    # The host is a constant, never a request parameter — the only redirect
    # target Diffuse will ever emit here is loopback.
    return RedirectResponse(
        build_loopback_redirect(port=claimed.callback_port, token=session_token),
        status_code=302,
        headers=dict(_BROWSER_HEADERS),
    )


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

    claimed = await _in_transaction(
        consume_oauth_state,
        state=raw_state,
        purpose=APP_INSTALL_PURPOSE,
    )
    if claimed is None or claimed.user_id is None:
        return _unlinked_install_page()

    # The state proves *a* user started an install. It does NOT prove this
    # `installation_id` is the one they installed — that value is a query
    # parameter under the caller's control, and installation ids are small
    # sequential integers. Anyone may sign in, so an attacker can mint a valid
    # state of their own and then hand-craft this URL with a victim's id.
    #
    # Persisting that claim would seed the table tenancy is going to read
    # (DEV-213) with an attacker-chosen row, so the linkage is deliberately not
    # written until it can be verified against GitHub. Verification needs either
    # an App JWT calling GET /app/installations/{id} — Diffuse holds no app
    # private key today — or the HMAC-signed `installation.created` webhook,
    # whose `sender` is authoritative and unspoofable. Tracked in DEV-226.
    LOGGER.info(
        "Received an unverified GitHub App setup redirect for installation %s; "
        "not linking it to a user until the installer can be verified",
        installation,
    )
    return _page(
        status_code=200,
        title="Installed",
        heading="Installed",
        body=(
            "<p>Diffuse has the installation. You can close this window.</p>"
            "<p>Repository access is confirmed separately, so it may take a "
            "moment to appear.</p>"
        ),
    )


def _unlinked_install_page() -> HTMLResponse:
    # Failing closed matters here: guessing an owner for an unattributed
    # installation would hand one user's repositories to another.
    return _error_page(
        status_code=400,
        heading="That installation could not be linked",
        detail=(
            "Sign in with `diffuse login` first, then start the install from "
            "Diffuse so it can be attributed to your account."
        ),
    )
