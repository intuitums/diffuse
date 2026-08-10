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

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import webhook_secret
from .github import (
    GitHubSetupError,
    exchange_oauth_code,
    mint_installation_token,
    oauth_authorize_url,
)
from .store import (
    acknowledge_event,
    authenticate_instance,
    consume_oauth_state,
    create_enrollment_code,
    create_oauth_state,
    instance_status,
    pull_events,
    record_verified_installation,
    record_webhook_event,
    redeem_enrollment_code,
    revoke_instance,
    set_installation_active,
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


def _connection_success_page(*, installation_id: int, connection_code: str) -> str:
    safe_code = html.escape(connection_code)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Diffuse GitHub connection</title>
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
  </style>
</head>
<body>
  <main>
    <h1>Diffuse</h1>
    <p>Installation <strong>{installation_id}</strong> is verified. Claim this
    one-time connection code on the self-hosted instance within 15 minutes:</p>
    <code class="code">{safe_code}</code>
    <pre class="code">diffuse github connect '{safe_code}' \\
  --name '&lt;instance-name&gt;' \\
  --write-env /path/to/github-integration.env</pre>
    <p class="hint">Prefer <code>--write-env</code> so the secrets are written once
    with mode 0600 instead of printed to stdout. Then run
    <code>diffuse github status</code> to confirm the instance is ready.</p>
  </main>
</body>
</html>
"""


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/github/setup")
async def github_setup(installation_id: int) -> RedirectResponse:
    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid installation")
    state = create_oauth_state(installation_id)
    return RedirectResponse(oauth_authorize_url(state), status_code=302)


@app.get("/auth/github/callback", response_model=None)
async def github_callback(
    request: Request,
    code: str,
    state: str,
):
    installation_id = consume_oauth_state(state)
    if installation_id is None:
        raise HTTPException(status_code=400, detail="Setup state is invalid or expired")
    try:
        user_id, login, installations = exchange_oauth_code(code)
    except GitHubSetupError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if installation_id not in installations:
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
            "Run diffuse github connect with this one-time code on the self-hosted instance."
        ),
    }
    accept = (request.headers.get("accept") or "").lower()
    prefers_json = "application/json" in accept and "text/html" not in accept
    if prefers_json:
        return JSONResponse(payload)
    return HTMLResponse(
        _connection_success_page(
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
