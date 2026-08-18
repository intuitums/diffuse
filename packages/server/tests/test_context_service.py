from __future__ import annotations

import httpx
import pytest
from diffuse.investigation import context_service


@pytest.mark.anyio
async def test_gateway_exposes_no_application_routes_to_runners():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context_service.app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/api/v1/repositories")).status_code == 404
        assert (await client.post("/agent/v1/tools/get-file", json={})).status_code == 404


@pytest.mark.anyio
async def test_gateway_bounds_tool_request_size_before_upstream_access():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context_service.app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            context_service.ALLOWED_PATH,
            content=b"x" * (context_service.MAX_REQUEST_BYTES + 1),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
