"""HTTP contract shared by the hosted relay and outbound Diffuse nodes."""

from __future__ import annotations

import base64
import json
import logging
from contextlib import closing
from functools import partial

import anyio
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from indexer.store import get_conn
from service.github import verify_signature
from service.github_app import (
    GitHubAppError,
    mint_installation_token_for_installation,
)
from service.relay_store import (
    DELIVERY_ID_PATTERN,
    InvalidPairingCodeError,
    RelayDeliveryConflictError,
    RelayNode,
    acknowledge_delivery,
    authenticate_node,
    exchange_pairing_code,
    lease_next_delivery,
    record_github_delivery,
    record_github_installation_event,
)

LOGGER = logging.getLogger(__name__)
router = APIRouter()
MAX_WEBHOOK_BODY_BYTES = 1_000_000
MAX_AUTHORIZATION_CHARS = 1024


class PairNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=40, max_length=128)
    name: str = Field(min_length=1, max_length=255)


class PairNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(
        default="diffuse-relay-pair-v1",
        serialization_alias="schemaVersion",
    )
    node_token: str = Field(serialization_alias="nodeToken")
    node_id: int = Field(gt=0, serialization_alias="nodeId")
    github_installation_id: int = Field(
        gt=0,
        serialization_alias="githubInstallationId",
    )


async def _in_transaction(callback, /, **kwargs):
    def run():
        with closing(get_conn()) as conn, conn:
            return callback(conn, **kwargs)

    return await anyio.to_thread.run_sync(run)


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Content-Length is invalid",
            ) from error
        if declared < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Content-Length is invalid",
            )
        if declared > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Request body exceeds the relay limit",
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Request body exceeds the relay limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _bearer_token(authorization: str) -> str:
    if (
        not isinstance(authorization, str)
        or not 1 <= len(authorization) <= MAX_AUTHORIZATION_CHARS
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Relay node authentication is required",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token or any(character.isspace() for character in token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Relay node authentication is required",
        )
    return token


async def _authenticated_node(authorization: str) -> RelayNode:
    node = await _in_transaction(
        authenticate_node,
        token=_bearer_token(authorization),
    )
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Relay node credential is invalid or revoked",
        )
    return node


@router.post(
    "/relay/v1/pair",
    response_model=PairNodeResponse,
    response_model_by_alias=True,
    tags=["Integration relay"],
)
async def pair_node(payload: PairNodeRequest, response: Response):
    try:
        node, token = await _in_transaction(
            exchange_pairing_code,
            code=payload.code,
            node_name=payload.name,
        )
    except (InvalidPairingCodeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pairing code is invalid or expired",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return PairNodeResponse(
        node_token=token,
        node_id=node.id,
        github_installation_id=node.github_installation_id,
    )


@router.post(
    "/relay/v1/deliveries/next",
    tags=["Integration relay"],
)
async def next_delivery(
    response: Response,
    authorization: str = Header(default=""),
):
    node = await _authenticated_node(authorization)
    delivery = await _in_transaction(lease_next_delivery, node=node)
    response.headers["Cache-Control"] = "no-store"
    if delivery is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return {
        "schemaVersion": "diffuse-relay-delivery-v1",
        "id": delivery.id,
        "provider": delivery.provider,
        "deliveryId": delivery.provider_delivery_id,
        "event": delivery.event_name,
        "bodyBase64": base64.b64encode(delivery.payload).decode("ascii"),
        "bodySha256": delivery.payload_sha256,
        "attempt": delivery.attempt_count,
        "leasedUntil": delivery.leased_until.isoformat(),
    }


@router.post(
    "/relay/v1/deliveries/{delivery_id}/ack",
    tags=["Integration relay"],
)
async def ack_delivery(
    delivery_id: int,
    authorization: str = Header(default=""),
):
    node = await _authenticated_node(authorization)
    if delivery_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Relay delivery was not found",
        )
    acknowledged = await _in_transaction(
        acknowledge_delivery,
        node=node,
        delivery_id=delivery_id,
    )
    if not acknowledged:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Relay delivery is not leased to this node",
        )
    return {"status": "acknowledged", "id": delivery_id}


@router.post(
    "/relay/v1/github/token",
    tags=["Integration relay"],
)
async def github_installation_token(
    response: Response,
    authorization: str = Header(default=""),
):
    node = await _authenticated_node(authorization)
    try:
        token, expires_in = await anyio.to_thread.run_sync(
            partial(
                mint_installation_token_for_installation,
                node.github_installation_id,
            )
        )
    except GitHubAppError as error:
        LOGGER.error(
            "Could not mint a GitHub token for relay node %s installation %s: %s",
            node.id,
            node.github_installation_id,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub installation token is temporarily unavailable",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return {
        "schemaVersion": "diffuse-relay-github-token-v1",
        "token": token,
        "expiresIn": expires_in,
    }


@router.post(
    "/webhook/github",
    tags=["Provider callbacks"],
)
async def github_gateway_webhook(
    request: Request,
    response: Response,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    body = await _read_bounded_body(request)
    verify_signature(
        body,
        x_hub_signature_256,
        request.app.state.github_webhook_secret,
    )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body is not valid JSON",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body must be a JSON object",
        )
    if x_github_event == "ping":
        return {"status": "ok"}
    try:
        installation_id = payload["installation"]["id"]
    except (KeyError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub App webhook has no installation identity",
        ) from error
    if (
        isinstance(installation_id, bool)
        or not isinstance(installation_id, int)
        or installation_id <= 0
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub App webhook installation identity is invalid",
        )
    try:
        if x_github_event == "installation":
            if not DELIVERY_ID_PATTERN.fullmatch(x_github_delivery):
                raise ValueError("GitHub delivery id is invalid")
            await _in_transaction(
                record_github_installation_event,
                payload=payload,
            )
            # Installation lifecycle is gateway routing state, not node review
            # work. Persisting it in the delivery queue would strand a raw
            # `deleted` or `suspend` body immediately after the node is revoked.
            response.status_code = status.HTTP_202_ACCEPTED
            return {
                "status": "accepted",
                "delivery": x_github_delivery,
                "installation": installation_id,
            }
        accepted = await _in_transaction(
            record_github_delivery,
            github_installation_id=installation_id,
            provider_delivery_id=x_github_delivery,
            event_name=x_github_event,
            payload=body,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except RelayDeliveryConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Webhook delivery conflicts with previously accepted content",
        ) from error
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "accepted" if accepted else "deduplicated",
        "delivery": x_github_delivery,
        "installation": installation_id,
    }
