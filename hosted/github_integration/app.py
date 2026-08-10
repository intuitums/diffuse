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
from typing import Annotated

from fastapi import FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import public_url, webhook_secret
from .github import (
    GitHubInstallationSummary,
    GitHubSetupError,
    exchange_oauth_code,
    github_app_install_url,
    mint_installation_token,
    oauth_authorize_url,
)
from .store import (
    acknowledge_event,
    authenticate_instance,
    authorize_connect_session,
    claim_connect_session,
    consume_oauth_state,
    create_connect_session,
    create_oauth_state,
    get_connect_session,
    mark_connect_session_ready,
    pull_events,
    record_verified_installation,
    record_webhook_event,
    redeem_enrollment_code,
)

MAX_WEBHOOK_BYTES = 1_000_000

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _instance_from_authorization(authorization: str) -> object:
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing instance credential")
    instance = authenticate_instance(authorization[len(prefix) :])
    if instance is None:
        raise HTTPException(status_code=401, detail="Invalid instance credential")
    return instance


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


def _page(*, title: str, body: str) -> str:
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
      border: 1px solid #2f5d4a;
      background: #2f5d4a;
      color: #f7fbf9;
      font: inherit;
      padding: 0.85rem 1rem;
      text-align: left;
      text-decoration: none;
      cursor: pointer;
    }}
    button.secondary, .button.secondary {{
      background: #fbfdfc;
      color: #14201b;
    }}
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


def _run_cli_page(*, installation_id: int) -> str:
    return _page(
        title="Diffuse GitHub connection",
        body=f"""
    <p>Installation <strong>{installation_id}</strong> is verified on the
    Diffuse GitHub App.</p>
    <p>Finish connecting from the self-hosted instance — do not copy a code from
    this page:</p>
    <pre class="code">diffuse github connect --name '&lt;instance-name&gt;' \\
  --write-env /path/to/github-integration.env</pre>
    <p class="hint">The CLI opens this browser flow, waits until the installation
    is selected, then writes one-time credentials with mode 0600.</p>
""",
    )


def _return_to_cli_page(*, installation_id: int) -> str:
    return _page(
        title="Diffuse GitHub connection",
        body=f"""
    <p>Installation <strong>{installation_id}</strong> is ready.</p>
    <p>Return to the terminal where <code>diffuse github connect</code> is
    running. It will finish writing credentials automatically.</p>
    <p class="hint">You can close this tab.</p>
""",
    )


def _install_app_page(*, install_url: str) -> str:
    safe_url = html.escape(install_url, quote=True)
    return _page(
        title="Install Diffuse GitHub App",
        body=f"""
    <p>No GitHub App installation is available for the authorized account yet.</p>
    <p>Install the Diffuse GitHub App, then this same connect session will
    continue automatically.</p>
    <p class="actions"><a class="button" href="{safe_url}">Install Diffuse GitHub App</a></p>
    <p class="hint">Keep the <code>diffuse github connect</code> command running
    while you finish installation.</p>
""",
    )


def _picker_page(
    *,
    session_id: str,
    selection_token: str,
    installations: tuple[GitHubInstallationSummary, ...],
) -> str:
    safe_session = html.escape(session_id, quote=True)
    safe_token = html.escape(selection_token, quote=True)
    options = []
    for item in installations:
        label = html.escape(f"{item.account_login} ({item.account_type})")
        options.append(
            f"""<button type="submit" name="installation_id" value="{item.id}">
        Use {label}
      </button>"""
        )
    joined = "\n".join(options)
    return _page(
        title="Choose GitHub installation",
        body=f"""
    <p>Choose which GitHub App installation this self-hosted Diffuse instance
    should use.</p>
    <form class="actions" method="post" action="/connect/{safe_session}/installations">
      <input type="hidden" name="selection_token" value="{safe_token}">
      {joined}
    </form>
""",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


class ConnectSessionRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


@app.post("/v1/connect/sessions")
async def connect_sessions_create(payload: ConnectSessionRequest) -> dict[str, object]:
    try:
        created = create_connect_session(
            display_name=payload.display_name,
            public_base_url=public_url(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "session_id": created.session_id,
        "poll_token": created.poll_token,
        "browser_url": created.browser_url,
        "expires_in_seconds": created.expires_in_seconds,
    }


@app.get("/v1/connect/sessions/{session_id}")
async def connect_sessions_poll(
    session_id: str,
    authorization: Annotated[str, Header()] = "",
) -> dict[str, object]:
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing connect session credential")
    poll_token = authorization[len(prefix) :].strip()
    if not poll_token:
        raise HTTPException(status_code=401, detail="Missing connect session credential")
    status_name, credentials = claim_connect_session(session_id, poll_token=poll_token)
    if status_name == "not_found":
        raise HTTPException(status_code=404, detail="Connect session not found")
    if status_name == "unauthorized":
        raise HTTPException(status_code=401, detail="Invalid connect session credential")
    if status_name == "pending":
        return {"status": "pending"}
    if status_name == "expired":
        return {"status": "expired"}
    if status_name == "consumed":
        return {"status": "consumed"}
    if status_name == "failed" or credentials is None:
        return {"status": "failed"}
    return {
        "status": "ready",
        "instance_id": credentials.instance_id,
        "installation_id": credentials.installation_id,
        "instance_token": credentials.instance_token,
        "event_signing_key": credentials.event_signing_key,
        "secrets_shown_once": True,
    }


@app.get("/connect/{session_id}", response_model=None)
async def connect_browser(session_id: str):
    session = get_connect_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Connect session not found")
    if session.expired and session.status not in {"ready", "consumed"}:
        return HTMLResponse(
            _page(
                title="Connect session expired",
                body="<p>This connect session expired. Run "
                "<code>diffuse github connect</code> again.</p>",
            ),
            status_code=410,
        )
    if session.status in {"ready", "consumed"} and session.github_installation_id is not None:
        return HTMLResponse(
            _return_to_cli_page(installation_id=session.github_installation_id)
        )
    state = create_oauth_state(connect_session_id=session_id)
    return RedirectResponse(oauth_authorize_url(state), status_code=302)


@app.post("/connect/{session_id}/installations", response_model=None)
async def connect_select_installation(
    session_id: str,
    installation_id: Annotated[int, Form()],
    selection_token: Annotated[str, Form()],
):
    session = get_connect_session(session_id)
    if session is None or session.expired:
        raise HTTPException(status_code=400, detail="Connect session is invalid or expired")
    if session.github_user_id is None or session.github_login is None:
        raise HTTPException(status_code=400, detail="Connect session is not authorized yet")
    try:
        record_verified_installation(
            installation_id,
            github_user_id=session.github_user_id,
            github_login=session.github_login,
        )
        mark_connect_session_ready(
            session_id,
            installation_id=installation_id,
            selection_token=selection_token,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return HTMLResponse(_return_to_cli_page(installation_id=installation_id))


def _complete_session_installation(
    *,
    session_id: str,
    installation_id: int,
    user_id: int,
    login: str,
    installations: tuple[GitHubInstallationSummary, ...],
) -> HTMLResponse:
    allowed_ids = {item.id for item in installations}
    if installation_id not in allowed_ids:
        raise HTTPException(
            status_code=403,
            detail="The authorized user does not control this installation",
        )
    record_verified_installation(
        installation_id,
        github_user_id=user_id,
        github_login=login,
    )
    authorize_connect_session(
        session_id,
        github_user_id=user_id,
        github_login=login,
        installation_ids=tuple(allowed_ids),
    )
    mark_connect_session_ready(session_id, installation_id=installation_id)
    return HTMLResponse(_return_to_cli_page(installation_id=installation_id))


def _continue_session_after_oauth(
    *,
    session_id: str,
    user_id: int,
    login: str,
    installations: tuple[GitHubInstallationSummary, ...],
) -> HTMLResponse:
    if not installations:
        authorize_connect_session(
            session_id,
            github_user_id=user_id,
            github_login=login,
            installation_ids=(),
        )
        resume_state = create_oauth_state(connect_session_id=session_id)
        return HTMLResponse(
            _install_app_page(install_url=github_app_install_url(state=resume_state))
        )
    if len(installations) == 1:
        only = installations[0]
        return _complete_session_installation(
            session_id=session_id,
            installation_id=only.id,
            user_id=user_id,
            login=login,
            installations=installations,
        )
    selection_token = authorize_connect_session(
        session_id,
        github_user_id=user_id,
        github_login=login,
        installation_ids=tuple(item.id for item in installations),
    )
    return HTMLResponse(
        _picker_page(
            session_id=session_id,
            selection_token=selection_token,
            installations=installations,
        )
    )


@app.get("/auth/github/setup", response_model=None)
async def github_setup(installation_id: int, state: str | None = None):
    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid installation")
    connect_session_id: str | None = None
    if state:
        prior = consume_oauth_state(state)
        if prior is None:
            raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
        connect_session_id = prior.connect_session_id
    oauth_state = create_oauth_state(
        installation_id,
        connect_session_id=connect_session_id,
    )
    return RedirectResponse(oauth_authorize_url(oauth_state), status_code=302)


@app.get("/auth/github/callback", response_model=None)
async def github_callback(
    request: Request,
    code: str,
    state: str,
):
    oauth_state = consume_oauth_state(state)
    if oauth_state is None:
        raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
    try:
        user_id, login, installations = exchange_oauth_code(code)
    except GitHubSetupError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    installation_ids = {item.id for item in installations}

    if oauth_state.connect_session_id is not None and oauth_state.installation_id is not None:
        return _complete_session_installation(
            session_id=oauth_state.connect_session_id,
            installation_id=oauth_state.installation_id,
            user_id=user_id,
            login=login,
            installations=installations,
        )

    if oauth_state.connect_session_id is not None:
        return _continue_session_after_oauth(
            session_id=oauth_state.connect_session_id,
            user_id=user_id,
            login=login,
            installations=installations,
        )

    # Setup-URL-without-CLI: verify control, then send the operator to the CLI.
    if oauth_state.installation_id is None:
        raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
    if oauth_state.installation_id not in installation_ids:
        raise HTTPException(
            status_code=403,
            detail="The authorized user does not control this installation",
        )
    record_verified_installation(
        oauth_state.installation_id,
        github_user_id=user_id,
        github_login=login,
    )
    payload = {
        "status": "verified",
        "installation_id": oauth_state.installation_id,
        "next": (
            "Run diffuse github connect --name <instance-name> --write-env PATH "
            "on the self-hosted instance. The CLI opens a browser and finishes "
            "without a copy-pasted connection code."
        ),
    }
    accept = (request.headers.get("accept") or "").lower()
    prefers_json = "application/json" in accept and "text/html" not in accept
    if prefers_json:
        return JSONResponse(payload)
    return HTMLResponse(_run_cli_page(installation_id=oauth_state.installation_id))


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
    instance = _instance_from_authorization(authorization)
    try:
        token, expires_at = mint_installation_token(instance.installation_id)
    except GitHubSetupError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {"token": token, "expires_at": expires_at}
