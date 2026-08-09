"""Narrow network gateway between untrusted runners and the control plane.

Docker networks isolate services, not HTTP paths.  The runner therefore never
shares a network with the application: this tiny credential-free proxy is the
only bridge, and it forwards one explicitly enumerated capability route.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

UPSTREAM = os.environ.get("DIFFUSE_REVIEW_AGENT_TOOL_UPSTREAM", "http://app:8000").rstrip("/")
ALLOWED_PATH = "/agent/v1/tools/search-code"
MAX_REQUEST_BYTES = 32_768

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(ALLOWED_PATH)
async def search_code(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Forward one capability-authenticated tool request, and nothing else."""

    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Request is too large",
        )
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=35) as client:
        upstream = await client.post(
            f"{UPSTREAM}{ALLOWED_PATH}", content=body, headers=headers
        )
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type,
    )
