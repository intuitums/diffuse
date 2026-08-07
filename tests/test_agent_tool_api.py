"""Gate B capability-tool and runner boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from service.agents import runner, tool_api, tool_server
from service.agents.contract import SessionScope, mint_session_capability

SIGNING_KEY = "s" * 48
HEAD_SHA = "a" * 40


def _capability(*, operations: frozenset[str] = frozenset({"search_code"})) -> str:
    return mint_session_capability(
        signing_key=SIGNING_KEY,
        runtime="claude",
        scope=SessionScope(
            repository_id=7,
            pull_request_id=11,
            snapshot_id=13,
            head_sha=HEAD_SHA,
            operations=operations,
        ),
        now=datetime.now(UTC),
    ).token


@pytest.mark.anyio
async def test_agent_search_requires_a_valid_search_capability(monkeypatch):
    app = FastAPI()
    app.include_router(tool_api.router)
    monkeypatch.setenv("DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY", SIGNING_KEY)
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        tool_api,
        "_search_code",
        lambda capability, request: (
            calls.append((capability, request))
            or {"schemaVersion": "diffuse-code-search-v1", "sources": []}
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        missing = await client.post("/agent/v1/tools/search-code", json={"query": "auth"})
        valid = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {_capability()}"},
            json={"query": "auth", "path": "service", "limit": 9},
        )

    assert missing.status_code == 401
    assert valid.status_code == 200
    assert valid.json()["sources"] == []
    assert len(calls) == 1
    capability, request = calls[0]
    assert capability.scope.repository_id == 7
    assert request.path == "service"
    assert request.limit == 9


@pytest.mark.anyio
async def test_agent_search_refuses_a_capability_without_the_tool_operation(monkeypatch):
    app = FastAPI()
    app.include_router(tool_api.router)
    monkeypatch.setenv("DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY", SIGNING_KEY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {_capability(operations=frozenset({'get_diff'}))}"},
            json={"query": "auth"},
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_runner_refuses_to_start_without_a_passing_compartment(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(runner, "preflight", lambda: calls.append(True))
    monkeypatch.setattr(runner, "validate_dispatch_public_key", lambda: None)

    async with runner.app.router.lifespan_context(runner.app):
        assert calls == [True]


@pytest.mark.anyio
async def test_capability_mcp_tool_sends_the_token_only_as_a_bearer_header(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"schemaVersion":"diffuse-code-search-v1","sources":[]}'

    monkeypatch.setattr(
        tool_server,
        "urlopen",
        lambda request, timeout: requests.append((request, timeout)) or Response(),
    )
    server = tool_server.create_capability_tool_server(
        "http://app:8000/agent/v1", "capability-secret"
    )
    tool = server._tool_manager.get_tool("search_code")
    result = await tool.run(
        {"query": "authorization", "path_prefix": "service", "limit": 1000},
        convert_result=False,
    )

    assert result == {"schemaVersion": "diffuse-code-search-v1", "sources": []}
    request, timeout = requests[0]
    assert request.full_url == "http://app:8000/agent/v1/tools/search-code"
    assert request.get_header("Authorization") == "Bearer capability-secret"
    assert request.data == b'{"query":"authorization","path":"service","limit":20}'
    assert timeout == 30
