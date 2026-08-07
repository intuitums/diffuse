"""Gate B capability-tool and runner boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from service.agents import runner, tool_api, tool_server
from service.agents.contract import SessionScope, mint_session_capability
from service.review.agent_compartment import AgentCompartmentError

SIGNING_KEY = "s" * 48
HEAD_SHA = "a" * 40


def _capability(**kwargs) -> str:
    values = {
        "repository_id": 7,
        "pull_request_id": 11,
        "snapshot_id": 13,
        "head_sha": HEAD_SHA,
        "operations": frozenset({"search_code"}),
    }
    values.update(kwargs)
    operations = values.pop("operations")
    now = values.pop("now", datetime.now(UTC))
    ttl = values.pop("ttl", timedelta(minutes=15))
    return mint_session_capability(
        signing_key=SIGNING_KEY,
        runtime="claude",
        scope=SessionScope(operations=operations, **values),
        now=now,
        ttl=ttl,
    ).token


@pytest.fixture
def tool_client(monkeypatch):
    app = FastAPI()
    app.include_router(tool_api.router)
    monkeypatch.setenv("DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY", SIGNING_KEY)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.mark.anyio
async def test_search_code_auth_and_errors(tool_client, monkeypatch):
    calls: list[object] = []

    def search(capability, request):
        calls.append((capability.scope.repository_id, request.path, request.limit))
        return {"schemaVersion": "diffuse-code-search-v1", "sources": []}

    monkeypatch.setattr(tool_api, "_search_code", search)

    async with tool_client as client:
        missing = await client.post("/agent/v1/tools/search-code", json={"query": "auth"})
        wrong_op = await client.post(
            "/agent/v1/tools/search-code",
            headers={
                "Authorization": f"Bearer {_capability(operations=frozenset({'get_diff'}))}"
            },
            json={"query": "auth"},
        )
        expired_token = _capability(
            now=datetime.now(UTC) - timedelta(hours=1),
            ttl=timedelta(minutes=1),
        )
        expired = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {expired_token}"},
            json={"query": "auth"},
        )
        ok = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {_capability()}"},
            json={"query": "auth", "path": "service", "limit": 9},
        )

    assert missing.status_code == 401
    assert wrong_op.status_code == 403
    assert expired.status_code == 401
    assert ok.status_code == 200
    assert calls == [(7, "service", 9)]


@pytest.mark.anyio
async def test_search_distinguishes_target_loss_from_bad_query(tool_client, monkeypatch):
    async with tool_client as client:
        def lost_target(*_a, **_k):
            raise tool_api.CapabilityTargetError("gone")

        monkeypatch.setattr(tool_api, "_search_code", lost_target)
        lost = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {_capability()}"},
            json={"query": "auth"},
        )

        def bad_query(*_a, **_k):
            raise ValueError("query must contain")

        monkeypatch.setattr(tool_api, "_search_code", bad_query)
        bad = await client.post(
            "/agent/v1/tools/search-code",
            headers={"Authorization": f"Bearer {_capability()}"},
            json={"query": "auth"},
        )

    assert lost.status_code == 403
    assert bad.status_code == 400


@pytest.mark.anyio
async def test_runner_daemon_preflight_fails_closed(monkeypatch):
    def boom(**_kwargs):
        raise AgentCompartmentError("blocked")

    monkeypatch.setattr(runner, "preflight", boom)
    with pytest.raises(AgentCompartmentError, match="blocked"):
        async with runner.app.router.lifespan_context(runner.app):
            pass


def test_agent_tool_url_requires_the_fixed_internal_port():
    with pytest.raises(ValueError, match="8011"):
        tool_server.validate_agent_tool_url("http://app:9999/agent/v1")
    assert (
        tool_server.validate_agent_tool_url("http://app:8011/agent/v1")
        == "http://app:8011/agent/v1"
    )


@pytest.mark.anyio
async def test_capability_mcp_transport(monkeypatch):
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
    with pytest.raises(ValueError, match="agent/v1"):
        tool_server.create_capability_tool_server(
            "http://evil.example/agent/v1", "capability-secret"
        )

    server = tool_server.create_capability_tool_server(
        "http://app:8011/agent/v1", "capability-secret"
    )
    tool = server._tool_manager.get_tool("search_code")
    result = await tool.run(
        {"query": "authorization", "path_prefix": "service", "limit": 1000},
        convert_result=False,
    )

    assert result == {"schemaVersion": "diffuse-code-search-v1", "sources": []}
    request, timeout = requests[0]
    assert request.full_url == "http://app:8011/agent/v1/tools/search-code"
    assert request.get_header("Authorization") == "Bearer capability-secret"
    assert request.data == b'{"query":"authorization","path":"service","limit":20}'
    assert timeout == 30
