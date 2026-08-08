"""Gate B capability-tool and runner boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from service.agents import runner, tool_api, tool_server
from service.agents.contract import AgentSessionResult, SessionScope, mint_session_capability
from service.review.workspace import SourceArtifact, build_source_artifact
from service.storage.agent_session import AgentSessionReviewAttempt

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


def test_runner_materializes_only_the_signed_read_only_workspace(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("result = value.method()\n")
    artifact = build_source_artifact(source)
    dispatch = SimpleNamespace(
        runtime="codex",
        diff_text="diff --git a/app.py b/app.py\n",
        source_artifact=artifact,
        capability="capability",
        session_id="session-1",
        capability_id="capability-1",
    )
    captured: dict[str, object] = {}

    def run_structured(_cli, _result_type, **kwargs):
        workspace = kwargs["workspace"]
        captured["workspace"] = workspace
        captured["content"] = (workspace / "app.py").read_text()
        captured["mode"] = (workspace / "app.py").stat().st_mode & 0o777
        captured["prompt"] = kwargs["user_prompt"]
        return (
            AgentSessionResult(
                runtime="codex",
                summary="No actionable issues found.",
                risk_score=0,
                audit_reference="session-1",
            ),
            3,
            5,
        )

    monkeypatch.setenv("DIFFUSE_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("DIFFUSE_AGENT_TOOL_URL", "http://agent-tool-gateway:8011/agent/v1")
    monkeypatch.setattr(runner, "verify_dispatch", lambda _envelope: dispatch)
    monkeypatch.setattr(runner, "resolve_cli", lambda _runtime: object())
    monkeypatch.setattr(runner, "assert_compartment", lambda *_args: object())
    monkeypatch.setattr(runner, "run_structured", run_structured)

    result = runner.review(runner.ReviewInvocation(envelope="signed"))

    assert result["prompt_tokens"] == 3
    assert captured["workspace"].name == "workspace"
    assert captured["content"] == "result = value.method()\n"
    assert captured["mode"] == 0o444
    assert "supplied, read-only repository workspace" in captured["prompt"]


def test_runner_rejects_an_invalid_workspace_before_starting_the_cli(monkeypatch):
    dispatch = SimpleNamespace(
        runtime="codex",
        diff_text="diff --git a/app.py b/app.py\n",
        source_artifact=SourceArtifact(b"not a tar archive"),
        capability="capability",
        session_id="session-1",
        capability_id="capability-1",
    )
    monkeypatch.setenv("DIFFUSE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr(runner, "verify_dispatch", lambda _envelope: dispatch)
    monkeypatch.setattr(runner, "resolve_cli", lambda _runtime: object())
    monkeypatch.setattr(runner, "assert_compartment", lambda *_args: object())
    monkeypatch.setattr(
        runner,
        "run_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CLI must not start")),
    )

    with pytest.raises(HTTPException) as raised:
        runner.review(runner.ReviewInvocation(envelope="signed"))

    assert getattr(raised.value, "status_code", None) == 400


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


def test_remote_search_persists_the_capability_pinned_call(monkeypatch):
    """The runner's remote lookup has the same durable evidence as local tools."""

    capability = tool_api.verify_session_capability(
        _capability(), signing_key=SIGNING_KEY
    )
    attempt_started_at = datetime.now(UTC)
    connection = _Connection()
    recorded: list[dict[str, object]] = []
    target = SimpleNamespace(context_plan=SimpleNamespace(fingerprint="b" * 64))
    result = {"schemaVersion": "diffuse-code-search-v1", "sources": []}

    monkeypatch.setattr(tool_api, "get_conn", lambda: connection)
    monkeypatch.setattr(
        tool_api,
        "resolve_agent_session_review_attempt",
        lambda _conn, *, capability: AgentSessionReviewAttempt(41, attempt_started_at),
    )
    monkeypatch.setattr(
        tool_api,
        "_target_for_capability",
        lambda _conn, _capability: target,
    )
    monkeypatch.setattr(tool_api, "search_codebase", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        tool_api,
        "record_review_tool_call",
        lambda _conn, review_run_id, **kwargs: recorded.append(
            {"review_run_id": review_run_id, **kwargs}
        ),
    )

    actual = tool_api._search_code(
        capability,
        tool_api.SearchCodeRequest(query="authorization", path="service", limit=9),
    )

    assert actual == result
    assert connection.rollbacks == 0
    assert len(recorded) == 1
    call = recorded[0]
    duration_ms = call.pop("duration_ms")
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert call == {
        "review_run_id": 41,
        "tool_name": "search_code",
        "arguments": {"query": "authorization", "path": "service", "limit": 9},
        "result": result,
        "index_snapshot_ids": (13,),
        "context_plan_fingerprint": "b" * 64,
        "attempt_started_at": attempt_started_at,
    }


def test_remote_search_records_a_failed_lookup_before_propagating(monkeypatch):
    capability = tool_api.verify_session_capability(
        _capability(), signing_key=SIGNING_KEY
    )
    attempt_started_at = datetime.now(UTC)
    connection = _Connection()
    recorded: list[dict[str, object]] = []

    monkeypatch.setattr(tool_api, "get_conn", lambda: connection)
    monkeypatch.setattr(
        tool_api,
        "resolve_agent_session_review_attempt",
        lambda _conn, *, capability: AgentSessionReviewAttempt(41, attempt_started_at),
    )
    monkeypatch.setattr(
        tool_api,
        "_target_for_capability",
        lambda _conn, _capability: (_ for _ in ()).throw(ValueError("snapshot unavailable")),
    )
    monkeypatch.setattr(
        tool_api,
        "record_review_tool_call",
        lambda _conn, review_run_id, **kwargs: recorded.append(
            {"review_run_id": review_run_id, **kwargs}
        ),
    )

    with pytest.raises(ValueError, match="snapshot unavailable"):
        tool_api._search_code(
            capability,
            tool_api.SearchCodeRequest(query="authorization"),
        )

    assert connection.rollbacks == 1
    assert len(recorded) == 1
    call = recorded[0]
    duration_ms = call.pop("duration_ms")
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert call == {
        "review_run_id": 41,
        "tool_name": "search_code",
        "arguments": {"query": "authorization", "path": None, "limit": 8},
        "failure_code": "search_failed",
        "failure_detail": "snapshot unavailable",
        "index_snapshot_ids": (13,),
        "context_plan_fingerprint": None,
        "attempt_started_at": attempt_started_at,
    }


def test_remote_search_refuses_an_unlinked_or_stale_session(monkeypatch):
    capability = tool_api.verify_session_capability(
        _capability(), signing_key=SIGNING_KEY
    )
    connection = _Connection()

    monkeypatch.setattr(tool_api, "get_conn", lambda: connection)
    monkeypatch.setattr(
        tool_api,
        "resolve_agent_session_review_attempt",
        lambda _conn, *, capability: None,
    )

    with pytest.raises(ValueError, match="no longer active"):
        tool_api._search_code(
            capability,
            tool_api.SearchCodeRequest(query="authorization"),
        )


class _Connection:
    """The connection behavior `tool_api._search_code` owns itself."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self) -> None:
        return None

    def rollback(self) -> None:
        self.rollbacks += 1
