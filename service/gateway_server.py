"""Minimal hosted callback and credential gateway for self-hosted Diffuse nodes."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing

import anyio
from fastapi import FastAPI, Response, status

from indexer.store import get_conn
from service.database_migrations import verify_database_current
from service.github_app import validate_gateway_app_configuration
from service.github_oauth import validate_gateway_oauth_configuration
from service.oauth_api import gateway_public_url
from service.oauth_api import router as oauth_router
from service.relay_api import router as relay_router


def _verify_gateway() -> None:
    validate_gateway_app_configuration()
    validate_gateway_oauth_configuration()
    gateway_public_url()
    with closing(get_conn()) as conn:
        verify_database_current(conn)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise ValueError("GITHUB_WEBHOOK_SECRET is required by the integration gateway")
    app.state.github_webhook_secret = secret
    await anyio.to_thread.run_sync(_verify_gateway)
    yield


app = FastAPI(
    title="Diffuse Integration Relay",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(relay_router)
app.include_router(oauth_router)


@app.get("/health")
async def health():
    return {"status": "ok", "role": "integration-relay"}


@app.get("/ready")
async def readiness(response: Response):
    try:
        await anyio.to_thread.run_sync(_verify_gateway)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ready", "role": "integration-relay"}
