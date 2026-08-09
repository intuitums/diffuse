"""The hosted Diffuse-Agent setup and webhook ingress API.

This service deliberately holds only installation binding and event-routing
metadata. Review payloads are relayed to the customer's Diffuse instance; all
execution and durable review state remains self-hosted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/github/setup")
async def github_setup(installation_id: int) -> RedirectResponse:
    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid installation")
    state = create_oauth_state(installation_id)
    return RedirectResponse(oauth_authorize_url(state), status_code=302)


@app.get("/auth/github/callback")
async def github_callback(code: str, state: str) -> JSONResponse:
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
    enrollment_code = create_enrollment_code(installation_id)
    return JSONResponse(
        {
            "status": "verified",
            "installation_id": installation_id,
            "enrollment_code": enrollment_code,
            "expires_in_seconds": 900,
            "next": (
                "Run diffuse hosted enroll with this one-time code on the self-hosted instance."
            ),
        }
    )


class EnrollmentClaim(BaseModel):
    code: str = Field(min_length=32, max_length=512)
    display_name: str = Field(min_length=1, max_length=200)


@app.post("/v1/enrollments/claim")
async def claim_enrollment(payload: EnrollmentClaim) -> dict[str, object]:
    credentials = redeem_enrollment_code(payload.code, display_name=payload.display_name)
    if credentials is None:
        raise HTTPException(status_code=400, detail="Enrollment code is invalid or expired")
    return {
        "instance_id": credentials.instance_id,
        "installation_id": credentials.installation_id,
        "instance_token": credentials.instance_token,
        "event_signing_key": credentials.event_signing_key,
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
