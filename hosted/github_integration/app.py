"""The GitHub Integration Service setup and webhook ingress API.

This service deliberately holds only installation binding and event-routing
metadata. Review payloads are relayed to the customer's Diffuse instance; all
execution and durable review state remains self-hosted.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
from typing import Annotated

from fastapi import FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .config import github_app_install_url, public_url, webhook_secret
from .github import (
    GitHubInstallation,
    GitHubSetupError,
    exchange_oauth_code,
    mint_installation_token,
    oauth_authorize_url,
)
from .store import (
    acknowledge_event,
    authenticate_instance,
    claim_connect_session,
    complete_connect_session,
    consume_oauth_state,
    create_connect_session,
    create_enrollment_code,
    create_oauth_state,
    fail_connect_session,
    get_pending_connect_session,
    instance_status,
    pull_events,
    record_verified_installation,
    record_webhook_event,
    redeem_enrollment_code,
    revoke_instance,
    set_connect_session_candidates,
    set_installation_active,
)

MAX_WEBHOOK_BYTES = 1_000_000
CONNECT_COOKIE = "diffuse_connect_session"
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _instance_from_authorization(authorization: str) -> object:
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing instance credential")
    instance = authenticate_instance(authorization[len(prefix) :])
    if instance is None:
        raise HTTPException(status_code=401, detail="Invalid instance credential")
    return instance


def _bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing instance credential")
    token = authorization[len(prefix) :].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing instance credential")
    return token


async def _bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > MAX_WEBHOOK_BYTES):
        raise HTTPException(status_code=413, detail="Webhook body exceeds the size limit")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body exceeds the size limit")
    return body


def _verify_github_signature(body: bytes, signature: str) -> None:
    expected = "sha256=" + hmac.new(webhook_secret().encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Missing or invalid webhook signature")


def _page_shell(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, #d7ebe3 0%, transparent 45%),
        linear-gradient(160deg, #f4f7f5 0%, #e7eee9 55%, #dfe8e2 100%);
      color: #14201b;
    }}
    main {{
      max-width: 40rem;
      margin: 0 auto;
      padding: 4rem 1.5rem;
    }}
    h1 {{
      font-family: "IBM Plex Serif", Georgia, serif;
      font-weight: 600;
      font-size: clamp(2rem, 4vw, 2.75rem);
      line-height: 1.1;
      margin: 0 0 0.75rem;
    }}
    p {{ line-height: 1.5; margin: 0 0 1rem; }}
    code, pre {{
      font-family: "IBM Plex Mono", ui-monospace, monospace;
    }}
    .code {{
      display: block;
      padding: 1rem 1.1rem;
      margin: 1.25rem 0;
      border: 1px solid #9eb5aa;
      background: #fbfdfc;
      overflow-x: auto;
      word-break: break-all;
      font-size: 0.95rem;
    }}
    .hint {{ color: #3c5348; font-size: 0.95rem; }}
    .actions {{ display: grid; gap: 0.75rem; margin: 1.5rem 0; }}
    button, .button {{
      appearance: none;
      border: 1px solid #1f3d32;
      background: #1f3d32;
      color: #f4faf7;
      font: inherit;
      padding: 0.85rem 1rem;
      text-align: left;
      text-decoration: none;
      cursor: pointer;
    }}
    button.secondary, a.secondary {{
      background: transparent;
      color: #1f3d32;
    }}
    details {{ margin-top: 2rem; color: #3c5348; }}
    summary {{ cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>Diffuse</h1>
    {body}
  </main>
</body>
</html>
"""


def _cli_success_page(*, installation_id: int, display_name: str) -> str:
    safe_name = html.escape(display_name)
    return _page_shell(
        "Diffuse connected",
        f"""
    <p>Installation <strong>{installation_id}</strong> is linked to
    <strong>{safe_name}</strong>.</p>
    <p>You can close this window. The CLI on your Diffuse host will finish
    writing credentials.</p>
    <p class="hint">Confirm with <code>diffuse github status</code>.</p>
""",
    )


def _install_needed_page() -> str:
    install_url = html.escape(github_app_install_url())
    return _page_shell(
        "Install Diffuse",
        f"""
    <p>Authorize succeeded, but this GitHub account has no Diffuse App
    installation yet.</p>
    <p>Install the App on the organization or user that owns your repositories,
    then GitHub will return here and finish the CLI connection.</p>
    <p class="actions"><a class="button" href="{install_url}">Install Diffuse GitHub App</a></p>
    <p class="hint">Keep this browser session open. The CLI is still waiting.</p>
""",
    )


def _pick_installation_page(
    *,
    session_id: str,
    installations: tuple[GitHubInstallation, ...] | tuple[dict[str, object], ...],
) -> str:
    options = []
    for item in installations:
        if isinstance(item, GitHubInstallation):
            installation_id = item.id
            login = item.account_login
            account_type = item.account_type
        else:
            installation_id = int(item["id"])
            login = str(item["account_login"])
            account_type = str(item.get("account_type") or "Organization")
        options.append(
            f"""
      <button type="submit" name="installation_id" value="{installation_id}">
        {html.escape(login)}
        <span class="hint">({html.escape(account_type)} · {installation_id})</span>
      </button>"""
        )
    safe_session = html.escape(session_id)
    return _page_shell(
        "Choose installation",
        f"""
    <p>Choose which GitHub App installation this Diffuse instance should use.</p>
    <form class="actions" method="post" action="/auth/github/connect/{safe_session}/select">
      {"".join(options)}
    </form>
""",
    )


def _setup_without_cli_page(*, installation_id: int, connection_code: str) -> str:
    safe_code = html.escape(connection_code)
    return _page_shell(
        "Diffuse GitHub connection",
        f"""
    <p>Installation <strong>{installation_id}</strong> is verified.</p>
    <p>On the machine that runs Diffuse, finish connecting from the CLI
    (preferred — opens this browser flow automatically):</p>
    <pre class="code">diffuse github connect --name '&lt;instance-name&gt;' \\
  --write-env /path/to/github-integration.env</pre>
    <p class="hint">If you already started that command in another window, return
    there — it will pick up this installation.</p>
    <details>
      <summary>Advanced: one-time connection code</summary>
      <p class="hint">Only needed if the CLI cannot open a browser.</p>
      <code class="code">{safe_code}</code>
      <pre class="code">diffuse github connect --code '{safe_code}' \\
  --name '&lt;instance-name&gt;' \\
  --write-env /path/to/github-integration.env</pre>
    </details>
""",
    )


def _set_connect_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        CONNECT_COOKIE,
        session_id,
        max_age=900,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_connect_cookie(response: Response) -> None:
    response.delete_cookie(CONNECT_COOKIE, path="/")


def _cookie_session_id(request: Request) -> str | None:
    value = request.cookies.get(CONNECT_COOKIE, "").strip()
    if not value or not _SESSION_ID_RE.fullmatch(value):
        return None
    return value


def _candidate_dicts(
    installations: tuple[GitHubInstallation, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "id": item.id,
            "account_login": item.account_login,
            "account_type": item.account_type,
        }
        for item in installations
    )


def _finish_connect_session(
    *,
    session_id: str,
    installation_id: int,
    github_user_id: int,
    github_login: str,
    allowed_installation_ids: set[int] | None,
) -> HTMLResponse:
    session = get_pending_connect_session(session_id)
    if session is None:
        raise HTTPException(status_code=400, detail="Connect session is invalid or expired")
    credentials = complete_connect_session(
        session_id,
        installation_id=installation_id,
        github_user_id=github_user_id,
        github_login=github_login,
        allowed_installation_ids=allowed_installation_ids,
    )
    if credentials is None:
        fail_connect_session(session_id, "Could not bind the selected installation")
        raise HTTPException(status_code=400, detail="Could not bind the selected installation")
    response = HTMLResponse(
        _cli_success_page(
            installation_id=credentials.installation_id,
            display_name=session.display_name,
        )
    )
    _clear_connect_cookie(response)
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


class ConnectSessionRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


@app.post("/v1/connect/sessions")
async def connect_sessions_create(payload: ConnectSessionRequest) -> dict[str, object]:
    try:
        created = create_connect_session(display_name=payload.display_name)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    browser_url = f"{public_url()}/auth/github/connect/{created.session_id}"
    return {
        "session_id": created.session_id,
        "poll_secret": created.poll_secret,
        "browser_url": browser_url,
        "expires_in_seconds": created.expires_in_seconds,
        "poll_path": f"/v1/connect/sessions/{created.session_id}",
    }


@app.get("/v1/connect/sessions/{session_id}")
async def connect_sessions_poll(
    session_id: str,
    authorization: Annotated[str, Header()] = "",
) -> dict[str, object]:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Connect session not found")
    poll_secret = _bearer_token(authorization)
    claim = claim_connect_session(session_id, poll_secret)
    if claim.status == "not_found":
        raise HTTPException(status_code=404, detail="Connect session not found")
    if claim.status == "pending":
        return {
            "status": "pending",
            "expires_in_seconds": claim.expires_in_seconds,
        }
    if claim.status == "ready" and claim.credentials is not None:
        return {
            "status": "ready",
            "instance_id": claim.credentials.instance_id,
            "installation_id": claim.credentials.installation_id,
            "instance_token": claim.credentials.instance_token,
            "event_signing_key": claim.credentials.event_signing_key,
            "secrets_shown_once": True,
        }
    if claim.status == "consumed":
        raise HTTPException(
            status_code=410,
            detail="Connect credentials were already claimed",
        )
    if claim.status == "expired":
        raise HTTPException(status_code=410, detail="Connect session expired")
    raise HTTPException(
        status_code=400,
        detail=claim.error_message or "Connect session failed",
    )


@app.get("/auth/github/connect/{session_id}", response_model=None)
async def github_connect_start(session_id: str) -> Response:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Connect session not found")
    session = get_pending_connect_session(session_id)
    if session is None:
        raise HTTPException(status_code=400, detail="Connect session is invalid or expired")
    if session.status == "ready":
        response = HTMLResponse(
            _cli_success_page(
                installation_id=session.installation_id or 0,
                display_name=session.display_name,
            )
        )
        _clear_connect_cookie(response)
        return response
    state = create_oauth_state(connect_session_id=session_id)
    response = RedirectResponse(oauth_authorize_url(state), status_code=302)
    _set_connect_cookie(response, session_id)
    return response


@app.post("/auth/github/connect/{session_id}/select", response_model=None)
async def github_connect_select(
    session_id: str,
    request: Request,
    installation_id: Annotated[int, Form()],
) -> Response:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Connect session not found")
    cookie_session = _cookie_session_id(request)
    if cookie_session != session_id:
        raise HTTPException(status_code=403, detail="Connect browser session mismatch")
    session = get_pending_connect_session(session_id)
    if session is None or session.status != "pending":
        raise HTTPException(status_code=400, detail="Connect session is invalid or expired")
    if session.authorized_github_user_id is None or not session.authorized_github_login:
        raise HTTPException(status_code=400, detail="Connect session is not ready for selection")
    allowed = {int(item["id"]) for item in session.candidate_installations}
    return _finish_connect_session(
        session_id=session_id,
        installation_id=installation_id,
        github_user_id=session.authorized_github_user_id,
        github_login=session.authorized_github_login,
        allowed_installation_ids=allowed,
    )


@app.get("/auth/github/setup", response_model=None)
async def github_setup(request: Request, installation_id: int) -> RedirectResponse:
    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid installation")
    session_id = _cookie_session_id(request)
    if session_id is not None and get_pending_connect_session(session_id) is not None:
        state = create_oauth_state(
            installation_id=installation_id,
            connect_session_id=session_id,
        )
        response = RedirectResponse(oauth_authorize_url(state), status_code=302)
        _set_connect_cookie(response, session_id)
        return response
    state = create_oauth_state(installation_id=installation_id)
    return RedirectResponse(oauth_authorize_url(state), status_code=302)


@app.get("/auth/github/callback", response_model=None)
async def github_callback(
    request: Request,
    code: str,
    state: str,
):
    target = consume_oauth_state(state)
    if target is None:
        raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
    try:
        user_id, login, installations = exchange_oauth_code(code)
    except GitHubSetupError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    installation_ids = {item.id for item in installations}

    if target.connect_session_id is not None:
        session_id = target.connect_session_id
        session = get_pending_connect_session(session_id)
        if session is None:
            raise HTTPException(status_code=400, detail="Connect session is invalid or expired")
        chosen = target.installation_id
        if chosen is not None:
            if chosen not in installation_ids:
                fail_connect_session(
                    session_id,
                    "The authorized user does not control this installation",
                )
                raise HTTPException(
                    status_code=403,
                    detail="The authorized user does not control this installation",
                )
            return _finish_connect_session(
                session_id=session_id,
                installation_id=chosen,
                github_user_id=user_id,
                github_login=login,
                allowed_installation_ids=installation_ids,
            )
        if not installations:
            response = HTMLResponse(_install_needed_page())
            _set_connect_cookie(response, session_id)
            return response
        if len(installations) == 1:
            only = installations[0]
            return _finish_connect_session(
                session_id=session_id,
                installation_id=only.id,
                github_user_id=user_id,
                github_login=login,
                allowed_installation_ids=installation_ids,
            )
        set_connect_session_candidates(
            session_id,
            candidates=_candidate_dicts(installations),
            github_user_id=user_id,
            github_login=login,
        )
        response = HTMLResponse(
            _pick_installation_page(session_id=session_id, installations=installations)
        )
        _set_connect_cookie(response, session_id)
        return response

    # Legacy setup-URL path (install App first, no CLI session yet).
    installation_id = target.installation_id
    if installation_id is None:
        raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
    if installation_id not in installation_ids:
        raise HTTPException(
            status_code=403,
            detail="The authorized user does not control this installation",
        )
    record_verified_installation(installation_id, github_user_id=user_id, github_login=login)
    connection_code = create_enrollment_code(installation_id)
    payload = {
        "status": "verified",
        "installation_id": installation_id,
        "connection_code": connection_code,
        "expires_in_seconds": 900,
        "next": (
            "Run diffuse github connect --name <instance> on the self-hosted "
            "instance (browser flow), or pass --code with this one-time code."
        ),
    }
    accept = (request.headers.get("accept") or "").lower()
    prefers_json = "application/json" in accept and "text/html" not in accept
    if prefers_json:
        return JSONResponse(payload)
    return HTMLResponse(
        _setup_without_cli_page(
            installation_id=installation_id,
            connection_code=connection_code,
        )
    )


class InstanceRegistration(BaseModel):
    code: str = Field(min_length=32, max_length=512)
    display_name: str = Field(min_length=1, max_length=200)


@app.post("/v1/instances/register")
async def register_instance(payload: InstanceRegistration) -> dict[str, object]:
    credentials = redeem_enrollment_code(payload.code, display_name=payload.display_name)
    if credentials is None:
        raise HTTPException(status_code=400, detail="Connection code is invalid or expired")
    # These secrets are returned exactly once. The integration service stores
    # only a peppered hash of the instance token and an AES-GCM sealed copy of
    # the delivery signing key; plaintext cannot be recovered later.
    return {
        "instance_id": credentials.instance_id,
        "installation_id": credentials.installation_id,
        "instance_token": credentials.instance_token,
        "event_signing_key": credentials.event_signing_key,
        "secrets_shown_once": True,
    }


@app.get("/v1/instances/me")
async def instances_me(authorization: Annotated[str, Header()] = "") -> dict[str, object]:
    token = _bearer_token(authorization)
    status_row = instance_status(token)
    if status_row is None:
        raise HTTPException(status_code=401, detail="Invalid instance credential")
    ready = status_row.installation_active
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "instance_id": status_row.instance_id,
        "installation_id": status_row.installation_id,
        "display_name": status_row.display_name,
        "installation_active": status_row.installation_active,
        "pending_events": status_row.pending_events,
        "created_at": status_row.created_at,
        "updated_at": status_row.updated_at,
        "diagnostic": (
            None
            if ready
            else "The GitHub App installation is suspended or inactive; resume it on GitHub."
        ),
    }


@app.post("/v1/instances/disconnect")
async def instances_disconnect(authorization: Annotated[str, Header()] = "") -> dict[str, str]:
    token = _bearer_token(authorization)
    if not revoke_instance(token):
        raise HTTPException(status_code=401, detail="Invalid instance credential")
    return {"status": "disconnected"}


def _apply_installation_lifecycle(event_name: str, payload: dict) -> str | None:
    """Mutate installation/instance state for lifecycle webhooks.

    Returns a terminal status when the delivery should not be queued for the
    self-hosted poller (uninstall already revoked the binding).
    """
    if event_name != "installation":
        return None
    action = payload.get("action")
    try:
        installation_id = int(payload["installation"]["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if action == "deleted":
        set_installation_active(installation_id, active=False, revoke_instances=True)
        return "installation_revoked"
    if action == "suspend":
        set_installation_active(installation_id, active=False, revoke_instances=False)
        return "installation_suspended"
    if action == "unsuspend":
        set_installation_active(installation_id, active=True, revoke_instances=False)
        return "installation_resumed"
    return None


@app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: Annotated[str, Header()] = "",
    x_github_delivery: Annotated[str, Header()] = "",
    x_hub_signature_256: Annotated[str, Header()] = "",
) -> dict[str, str]:
    body = await _bounded_body(request)
    _verify_github_signature(body, x_hub_signature_256)
    try:
        payload = json.loads(body)
        installation_id = int(payload["installation"]["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Webhook has no valid installation") from error
    if not x_github_delivery or not x_github_event:
        raise HTTPException(status_code=400, detail="Webhook delivery headers are required")
    lifecycle = _apply_installation_lifecycle(x_github_event, payload)
    if lifecycle is not None:
        return {"status": lifecycle}
    accepted = record_webhook_event(
        delivery_id=x_github_delivery,
        installation_id=installation_id,
        event_name=x_github_event,
        payload=payload,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
    return {"status": "accepted" if accepted else "deduplicated"}


@app.post("/v1/events/pull")
async def events_pull(
    authorization: Annotated[str, Header()] = "",
    limit: int = 20,
) -> dict[str, object]:
    instance = _instance_from_authorization(authorization)
    events = pull_events(instance, limit=limit)
    return {
        "events": [
            {
                "delivery_id": event.delivery_id,
                "event": event.event_name,
                "payload": event.payload,
                "signature": event.signature,
            }
            for event in events
        ]
    }


@app.post("/v1/events/{delivery_id}/ack")
async def events_acknowledge(
    delivery_id: str,
    authorization: Annotated[str, Header()] = "",
) -> dict[str, str]:
    instance = _instance_from_authorization(authorization)
    if not acknowledge_event(instance, delivery_id):
        raise HTTPException(status_code=404, detail="Event is not pending for this instance")
    return {"status": "acknowledged"}


@app.post("/v1/installation-token")
async def installation_token(authorization: Annotated[str, Header()] = "") -> dict[str, str]:
    token = _bearer_token(authorization)
    status_row = instance_status(token)
    if status_row is None:
        raise HTTPException(status_code=401, detail="Invalid instance credential")
    if not status_row.installation_active:
        raise HTTPException(
            status_code=403,
            detail="GitHub App installation is suspended or inactive",
        )
    try:
        minted, expires_at = mint_installation_token(status_row.installation_id)
    except GitHubSetupError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {"token": minted, "expires_at": expires_at}
