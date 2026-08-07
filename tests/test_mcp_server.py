import inspect
import threading

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from service import api_auth, mcp_server
from service.hosted import webhook_server
from service.hosted.api_tokens import (
    MCP_GENERATE_SCOPE,
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
    ServiceTokenAccess,
)
from service.hosted.webhook_server import app
from service.review import trigger as review_trigger
from service.scm import PullRequestEvent

API_TOKEN = "m" * 48


@pytest.mark.anyio
async def test_mcp_rejects_missing_and_incorrect_bearer_tokens(monkeypatch):
    monkeypatch.setenv("DIFFUSE_API_TOKEN", API_TOKEN)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "diffuse-test", "version": "1"},
        },
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        missing = await client.post("/mcp", json=initialize)
        incorrect = await client.post(
            "/mcp",
            json=initialize,
            headers={"Authorization": f"Bearer {'x' * 48}"},
        )
        health = await client.get("/health")

    assert missing.status_code == 401
    assert incorrect.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_mcp_initializes_lists_only_durable_tools_and_calls_one(
    monkeypatch,
):
    monkeypatch.setenv("DIFFUSE_API_TOKEN", API_TOKEN)
    # This is the only test that enters the app's lifespan, which now validates
    # the hot-path configuration the way the worker does. REVIEW_MODEL has no
    # default by design, so the API refuses to start without one.
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setattr(webhook_server, "_verify_database_schema", lambda: None)
    calls = []
    query_calls = []
    write_calls = []

    def database_query(callback, **kwargs):
        calls.append((callback, kwargs))
        if callback is mcp_server.resolve_code_query_target:
            return "resolved-query-target"
        if callback is not mcp_server.list_mcp_repositories:
            return {"ok": True}
        return {
            "repositories": [
                {
                    "id": 7,
                    "name": "owner/repo",
                    "remote": "github",
                    "activeSnapshot": {"id": 17, "commitSha": "a" * 40},
                }
            ],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    monkeypatch.setattr(mcp_server, "_database_query", database_query)
    monkeypatch.setattr(
        mcp_server,
        "_database_write",
        lambda callback, **kwargs: (
            write_calls.append((callback, kwargs))
            or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "search_codebase",
        lambda target, **kwargs: (
            query_calls.append(("search", target, kwargs))
            or {"schemaVersion": "diffuse-code-search-v1", "sources": []}
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "answer_codebase_query",
        lambda target, **kwargs: (
            query_calls.append(("answer", target, kwargs))
            or {
                "schemaVersion": "diffuse-code-answer-v1",
                "status": "insufficient_evidence",
            }
        ),
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            follow_redirects=True,
        ) as http_client,
        streamable_http_client(
            "http://testserver/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream, _session_id),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(
            "list_repositories",
            {"enabled": True, "limit": 5, "offset": 0},
        )
        analytics = await session.call_tool(
            "get_review_analytics",
            {
                "startAt": "2026-07-01T00:00:00Z",
                "endAt": "2026-08-01T00:00:00Z",
                "name": "owner/repo",
                "remote": "github",
                "defaultBranch": "main",
                "author": "octocat",
            },
        )
        code_search = await session.call_tool(
            "search_code",
            {
                "name": "owner/repo",
                "remote": "github",
                "defaultBranch": "main",
                "query": "tenant authorization",
                "path": "service",
                "includeRelated": True,
                "limit": 6,
            },
        )
        code_answer = await session.call_tool(
            "ask_codebase",
            {
                "name": "owner/repo",
                "remote": "github",
                "defaultBranch": "main",
                "question": "How is tenant authorization enforced?",
                "includeRelated": False,
                "limit": 5,
            },
        )
        pull_requests = await session.call_tool(
            "list_pull_requests",
            {
                "name": "owner/repo",
                "remote": "github",
                "defaultBranch": "main",
                "state": "open",
                "limit": 5,
                "offset": 0,
            },
        )
        comment_search = await session.call_tool(
            "search_review_comments",
            {
                "query": "authorization",
                "include_addressed": True,
                "limit": 4,
            },
        )
        fix_handoff = await session.call_tool(
            "get_fix_handoff",
            {
                "codeReviewId": "review_11",
                "findingFingerprint": "f" * 64,
                "agent": "codex",
            },
        )
        fix_all_handoff = await session.call_tool(
            "get_fix_all_handoff",
            {
                "codeReviewId": "review_11",
                "agent": "conductor",
            },
        )
        context_update = await session.call_tool(
            "update_custom_context",
            {
                "customContextId": "custom_context_9",
                "expectedUpdatedAt": "2026-07-23T18:00:00+00:00",
                "body": "Require tenant authorization.",
                "appliesTo": ["service/**"],
                "status": "inactive",
            },
        )
        context_delete = await session.call_tool(
            "delete_custom_context",
            {
                "customContextId": "custom_context_10",
                "expectedUpdatedAt": "2026-07-23T19:00:00+00:00",
            },
        )

    assert initialized.serverInfo.name == "Diffuse"
    assert {tool.name for tool in tools.tools} == {
        "list_repositories",
        "get_review_analytics",
        "search_code",
        "ask_codebase",
        "list_merge_requests",
        "list_pull_requests",
        "get_merge_request",
        "get_pull_request",
        "trigger_code_review",
        "list_code_reviews",
        "get_code_review",
        "get_fix_handoff",
        "get_fix_all_handoff",
        "list_merge_request_comments",
        "list_pull_request_comments",
        "search_review_comments",
        "list_custom_context",
        "get_custom_context",
        "search_custom_context",
        "create_custom_context",
        "update_custom_context",
        "delete_custom_context",
    }
    tools_by_name = {tool.name: tool for tool in tools.tools}
    assert {
        "startAt",
        "endAt",
        "name",
        "remote",
        "defaultBranch",
        "remoteUrl",
        "author",
    } == set(tools_by_name["get_review_analytics"].inputSchema["properties"])
    assert {
        "name",
        "remote",
        "defaultBranch",
        "remoteUrl",
        "state",
        "limit",
        "offset",
    } == set(tools_by_name["list_pull_requests"].inputSchema["properties"])
    assert {
        "codeReviewId",
        "findingFingerprint",
        "agent",
    } == set(tools_by_name["get_fix_handoff"].inputSchema["properties"])
    assert {
        "codeReviewId",
        "agent",
    } == set(tools_by_name["get_fix_all_handoff"].inputSchema["properties"])
    assert {
        "name",
        "remote",
        "defaultBranch",
        "query",
        "remoteUrl",
        "path",
        "includeRelated",
        "limit",
    } == set(tools_by_name["search_code"].inputSchema["properties"])
    assert {
        "name",
        "remote",
        "defaultBranch",
        "question",
        "remoteUrl",
        "path",
        "includeRelated",
        "limit",
    } == set(tools_by_name["ask_codebase"].inputSchema["properties"])
    assert {
        "customContextId",
        "expectedUpdatedAt",
        "body",
        "appliesTo",
        "type",
        "status",
        "metadata",
    } == set(
        tools_by_name["update_custom_context"].inputSchema["properties"]
    )
    assert {
        "customContextId",
        "expectedUpdatedAt",
    } == set(
        tools_by_name["delete_custom_context"].inputSchema["properties"]
    )
    assert not result.isError
    assert not analytics.isError
    assert not code_search.isError
    assert not code_answer.isError
    assert not pull_requests.isError
    assert not comment_search.isError
    assert not fix_handoff.isError
    assert not fix_all_handoff.isError
    assert not context_update.isError
    assert not context_delete.isError
    assert result.structuredContent == {
        "repositories": [
            {
                "id": 7,
                "name": "owner/repo",
                "remote": "github",
                "activeSnapshot": {"id": 17, "commitSha": "a" * 40},
            }
        ],
        "total": 1,
        "limit": 5,
        "offset": 0,
    }
    assert calls == [
        (
            mcp_server.list_mcp_repositories,
            {"enabled": True, "limit": 5, "offset": 0},
        ),
        (
            mcp_server.query_review_analytics,
            {
                "start_at": "2026-07-01T00:00:00Z",
                "end_at": "2026-08-01T00:00:00Z",
                "repository_name": "owner/repo",
                "remote": "github",
                "default_branch": "main",
                "remote_url": None,
                "author": "octocat",
            },
        ),
        (
            mcp_server.resolve_code_query_target,
            {
                "repository_name": "owner/repo",
                "remote": "github",
                "default_branch": "main",
                "remote_url": None,
                "include_related": True,
            },
        ),
        (
            mcp_server.resolve_code_query_target,
            {
                "repository_name": "owner/repo",
                "remote": "github",
                "default_branch": "main",
                "remote_url": None,
                "include_related": False,
            },
        ),
        (
            mcp_server.list_mcp_merge_requests,
            {
                "repository_name": "owner/repo",
                "remote": "github",
                "default_branch": "main",
                "remote_url": None,
                "state": "open",
                "limit": 5,
                "offset": 0,
            },
        ),
        (
            mcp_server.search_mcp_review_comments,
            {
                "query": "authorization",
                "repository_id": None,
                "include_addressed": True,
                "limit": 4,
                "offset": 0,
            },
        ),
        (
            mcp_server.get_mcp_fix_handoff,
            {
                "code_review_id": "review_11",
                "finding_fingerprint": "f" * 64,
                "agent": "codex",
            },
        ),
        (
            mcp_server.get_mcp_fix_all_handoff,
            {
                "code_review_id": "review_11",
                "agent": "conductor",
            },
        ),
    ]
    assert query_calls == [
        (
            "search",
            "resolved-query-target",
            {
                "query": "tenant authorization",
                "path_prefix": "service",
                "limit": 6,
            },
        ),
        (
            "answer",
            "resolved-query-target",
            {
                "question": "How is tenant authorization enforced?",
                "path_prefix": None,
                "limit": 5,
            },
        ),
    ]
    assert write_calls == [
        (
            mcp_server.update_custom_context_record,
            {
                "custom_context_id": "custom_context_9",
                "expected_updated_at": "2026-07-23T18:00:00+00:00",
                "context_type": None,
                "body": "Require tenant authorization.",
                "applies_to": ("service/**",),
                "status": "inactive",
                "metadata": None,
            },
        ),
        (
            mcp_server.delete_custom_context_record,
            {
                "custom_context_id": "custom_context_10",
                "expected_updated_at": "2026-07-23T19:00:00+00:00",
            },
        ),
    ]


@pytest.mark.anyio
async def test_diffuse_token_verifier_fails_closed_and_accepts_bootstrap_token(
    monkeypatch,
):
    verifier = mcp_server.DiffuseTokenVerifier()
    monkeypatch.setattr(api_auth, "_load_service_access", lambda _digest: None)

    monkeypatch.delenv("DIFFUSE_API_TOKEN", raising=False)
    assert await verifier.verify_token(API_TOKEN) is None
    monkeypatch.setenv("DIFFUSE_API_TOKEN", "short")
    assert await verifier.verify_token("short") is None
    monkeypatch.setenv("DIFFUSE_API_TOKEN", API_TOKEN)
    assert await verifier.verify_token("wrong" * 10) is None

    access = await verifier.verify_token(API_TOKEN)

    assert access is not None
    assert access.client_id == "diffuse-self-hosted"
    assert MCP_READ_SCOPE in access.scopes
    assert access.subject == "self-hosted-operator"
    assert access.claims == {
        "auth_kind": "bootstrap",
        "all_repositories": True,
        "repository_ids": [],
    }


@pytest.mark.anyio
async def test_diffuse_token_verifier_loads_scoped_non_recoverable_token(
    monkeypatch,
):
    verifier = mcp_server.DiffuseTokenVerifier()
    monkeypatch.delenv("DIFFUSE_API_TOKEN", raising=False)
    monkeypatch.setattr(
        api_auth,
        "_load_service_access",
        lambda _digest: ServiceTokenAccess(
            id=13,
            name="ide-agent",
            scopes=(MCP_READ_SCOPE,),
            all_repositories=False,
            repository_ids=(7, 9),
            expires_at=None,
        ),
    )

    access = await verifier.verify_token(API_TOKEN)

    assert access is not None
    assert access.client_id == "diffuse-service-token-13"
    assert access.scopes == [MCP_READ_SCOPE]
    assert access.subject == "ide-agent"
    assert access.claims == {
        "auth_kind": "service_token",
        "token_id": 13,
        "all_repositories": False,
        "repository_ids": [7, 9],
    }


def test_mcp_authorization_claims_are_fail_closed(monkeypatch):
    def access(claims):
        return AccessToken(
            token=API_TOKEN,
            client_id="test",
            scopes=[MCP_READ_SCOPE],
            claims=claims,
        )

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access(
            {
                "auth_kind": "service_token",
                "token_id": 3,
                "all_repositories": False,
                "repository_ids": [9, 7, 9],
            }
        ),
    )
    assert mcp_server._authorized_repository_ids() == frozenset({7, 9})

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access(
            {
                "auth_kind": "bootstrap",
                "all_repositories": True,
                "repository_ids": [],
            }
        ),
    )
    assert mcp_server._authorized_repository_ids() is None

    for claims in (
        None,
        {},
        {
            "auth_kind": "bootstrap",
            "all_repositories": False,
            "repository_ids": [1],
        },
        {
            "auth_kind": "service_token",
            "token_id": 0,
            "all_repositories": False,
            "repository_ids": [1],
        },
        {
            "auth_kind": "service_token",
            "token_id": 1,
            "all_repositories": True,
            "repository_ids": [1],
        },
        {
            "auth_kind": "service_token",
            "token_id": 1,
            "all_repositories": False,
            "repository_ids": [],
        },
        {
            "auth_kind": "service_token",
            "token_id": 1,
            "all_repositories": False,
            "repository_ids": [True],
        },
        {
            "auth_kind": "service_token",
            "token_id": 1,
            "all_repositories": False,
            "repository_ids": [-1],
        },
    ):
        monkeypatch.setattr(
            mcp_server,
            "get_access_token",
            lambda claims=claims: access(claims),
        )
        with pytest.raises(RuntimeError, match="authorization"):
            mcp_server._authorized_repository_ids()


def test_mcp_write_authorization_requires_scope_and_preserves_actor(monkeypatch):
    def access(scopes):
        return AccessToken(
            token=API_TOKEN,
            client_id="test",
            scopes=scopes,
            subject="ide-agent",
            claims={
                "auth_kind": "service_token",
                "token_id": 3,
                "all_repositories": False,
                "repository_ids": [7],
            },
        )

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access([MCP_READ_SCOPE]),
    )
    with pytest.raises(RuntimeError, match="write scope"):
        mcp_server._mcp_write_authorization()

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access([MCP_READ_SCOPE, MCP_WRITE_SCOPE]),
    )
    authorization = mcp_server._mcp_write_authorization()

    assert authorization.authorized_repository_ids == frozenset({7})
    assert authorization.actor_kind == "service_token"
    assert authorization.actor_label == "ide-agent"
    assert authorization.actor_token_id == 3


def test_mcp_codebase_answers_require_generation_scope(monkeypatch):
    def access(scopes):
        return AccessToken(
            token=API_TOKEN,
            client_id="test",
            scopes=scopes,
            claims={
                "auth_kind": "service_token",
                "token_id": 3,
                "all_repositories": False,
                "repository_ids": [7],
            },
        )

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access([MCP_READ_SCOPE]),
    )
    with pytest.raises(RuntimeError, match="generation scope"):
        mcp_server._require_mcp_generation_scope()

    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: access([MCP_READ_SCOPE, MCP_GENERATE_SCOPE]),
    )
    assert mcp_server._require_mcp_generation_scope() is None


@pytest.mark.anyio
async def test_mcp_review_trigger_fetches_authoritative_pr_and_queues_write(
    monkeypatch,
):
    authorization = mcp_server.McpWriteAuthorization(
        authorized_repository_ids=frozenset({7}),
        actor_kind="service_token",
        actor_label="ide-agent",
        actor_token_id=3,
    )
    target = {
        "repositoryId": 7,
        "name": "owner/repo",
        "remote": "github",
        "remoteUrl": "https://github.com",
        "defaultBranch": "main",
        "pullRequestNumber": 42,
        "headBranch": "feature/auth",
    }
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 42,
            "web_url": "https://github.com/owner/repo/pull/42",
            "action": "manual",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "mcp-test",
            "author": "developer",
            "base_branch": "main",
            "head_branch": "feature/auth",
            "is_draft": False,
            "labels": [],
            "title": "Protect tenant boundaries",
            "description": "Adds authorization checks.",
            "trigger_kind": "manual",
            "trigger_id": "mcp:test",
            "metadata_complete": True,
            "changed_file_count": 2,
            "state": "open",
            "source_created_at": "2026-07-23T14:00:00Z",
            "additions": 10,
            "deletions": 2,
        }
    )
    fetched = []
    writes = []

    async def fetch(request, **kwargs):
        fetched.append((request, kwargs))
        return event

    monkeypatch.setattr(
        mcp_server,
        "_mcp_write_authorization",
        lambda: authorization,
    )
    monkeypatch.setattr(
        mcp_server,
        "_database_query",
        lambda callback, **kwargs: target,
    )
    monkeypatch.setattr(mcp_server, "fetch_manual_pull_request_event", fetch)
    monkeypatch.setattr(
        mcp_server,
        "_database_write",
        lambda callback, **kwargs: (
            writes.append((callback, kwargs))
            or {"success": True, "jobId": 19}
        ),
    )

    result = await mcp_server.trigger_code_review(
        name="owner/repo",
        remote="github",
        defaultBranch="main",
        prNumber=42,
        branch="feature/auth",
    )

    assert result == {"success": True, "jobId": 19}
    assert fetched[0][0].repo_full_name == "owner/repo"
    assert fetched[0][0].requested_by == "ide-agent"
    assert fetched[0][1]["api_base_url"] == "https://api.github.com"
    assert writes == [
        (
            mcp_server.enqueue_mcp_review_trigger,
            {"event": event, "repository_id": 7},
        )
    ]


def test_github_api_base_url_keeps_enterprise_host_without_configured_api(monkeypatch):
    monkeypatch.setenv("GITHUB_WEB_URL", "https://ghe.corp.example")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)

    assert review_trigger.github_api_base_url("https://ghe.corp.example") == (
        "https://ghe.corp.example/api/v3"
    )
    assert review_trigger.github_api_base_url("https://other.corp.example/") == (
        "https://other.corp.example/api/v3"
    )


def test_github_api_base_url_uses_configured_primary_api(monkeypatch):
    monkeypatch.setenv("GITHUB_WEB_URL", "https://ghe.corp.example")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.ghe.corp.example/v3/")

    assert review_trigger.github_api_base_url("https://ghe.corp.example") == (
        "https://api.ghe.corp.example/v3"
    )


def test_github_api_base_url_maps_public_github_to_public_api(monkeypatch):
    monkeypatch.delenv("GITHUB_WEB_URL", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)

    assert review_trigger.github_api_base_url("https://github.com") == (
        "https://api.github.com"
    )


@pytest.mark.anyio
async def test_mcp_review_trigger_rechecks_branch_after_provider_fetch(monkeypatch):
    authorization = mcp_server.McpWriteAuthorization(
        authorized_repository_ids=frozenset({7}),
        actor_kind="service_token",
        actor_label="ide-agent",
        actor_token_id=3,
    )
    target = {
        "repositoryId": 7,
        "name": "owner/repo",
        "remote": "github",
        "remoteUrl": "https://github.com",
        "defaultBranch": "main",
        "pullRequestNumber": 42,
        "headBranch": "feature/old-name",
    }
    current_event = PullRequestEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        number=42,
        web_url="https://github.com/owner/repo/pull/42",
        action="manual",
        head_sha="a" * 40,
        base_sha="b" * 40,
        updated_at="2026-07-23T19:00:00Z",
        delivery_id="mcp-renamed-branch",
        author="developer",
        base_branch="main",
        head_branch="feature/current-name",
        title="Rename branch",
        trigger_kind="manual",
        trigger_id="mcp:renamed",
        metadata_complete=True,
        changed_file_count=1,
    )
    writes = []

    async def fetch(*args, **kwargs):
        return current_event

    monkeypatch.setattr(
        mcp_server,
        "_mcp_write_authorization",
        lambda: authorization,
    )
    monkeypatch.setattr(
        mcp_server,
        "_database_query",
        lambda callback, **kwargs: target,
    )
    monkeypatch.setattr(
        mcp_server,
        "fetch_manual_pull_request_event",
        fetch,
    )
    monkeypatch.setattr(
        mcp_server,
        "_database_write",
        lambda callback, **kwargs: writes.append((callback, kwargs)),
    )

    with pytest.raises(ValueError, match="current PR/MR head"):
        await mcp_server.trigger_code_review(
            name="owner/repo",
            remote="github",
            defaultBranch="main",
            prNumber=42,
            branch="feature/old-name",
        )
    assert writes == []


def test_database_query_injects_repository_authorization(monkeypatch):
    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    received = {}

    def callback(conn, **kwargs):
        received.update(kwargs)
        assert conn is connection
        return "ok"

    monkeypatch.setattr(mcp_server, "get_conn", lambda: connection)
    monkeypatch.setattr(
        mcp_server,
        "_authorized_repository_ids",
        lambda: frozenset({7, 9}),
    )

    assert mcp_server._database_query(callback, limit=3) == "ok"
    assert received == {
        "authorized_repository_ids": frozenset({7, 9}),
        "limit": 3,
    }
    assert connection.closed


def test_every_registered_mcp_tool_is_async():
    tools = mcp_server.diffuse_mcp._tool_manager.list_tools()
    assert len(tools) >= 20

    # FastMCP runs a synchronous tool inline on the event loop the webhook app shares,
    # so a blocking tool stalls deliveries and the /ready healthcheck until it returns.
    blocking = sorted(
        tool.name for tool in tools if not inspect.iscoroutinefunction(tool.fn)
    )
    assert blocking == []
    assert all(tool.is_async for tool in tools)


@pytest.mark.anyio
async def test_mcp_tool_runs_blocking_database_work_off_the_event_loop(monkeypatch):
    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    callback_threads = []

    def list_repositories(conn, **kwargs):
        callback_threads.append(threading.get_ident())
        return {"repositories": []}

    monkeypatch.setattr(mcp_server, "get_conn", lambda: connection)
    monkeypatch.setattr(mcp_server, "list_mcp_repositories", list_repositories)
    monkeypatch.setattr(
        mcp_server,
        "_authorized_repository_ids",
        lambda: frozenset({7}),
    )

    result = await mcp_server.list_repositories(enabled=True, limit=5, offset=0)

    assert result == {"repositories": []}
    assert len(callback_threads) == 1
    assert callback_threads[0] != threading.get_ident()
    assert connection.closed


def test_mcp_public_url_and_host_configuration_are_strict(monkeypatch):
    monkeypatch.setenv("DIFFUSE_PUBLIC_URL", "https://diffuse.example.com/")
    monkeypatch.setenv(
        "DIFFUSE_MCP_ALLOWED_HOSTS",
        "diffuse.example.com,localhost:*",
    )
    assert mcp_server._public_url() == "https://diffuse.example.com"
    assert mcp_server._allowed_hosts() == [
        "diffuse.example.com",
        "localhost:*",
    ]

    monkeypatch.setenv("DIFFUSE_PUBLIC_URL", "https://user@diffuse.example.com/api")
    with pytest.raises(ValueError, match="HTTP"):
        mcp_server._public_url()
    monkeypatch.setenv("DIFFUSE_MCP_ALLOWED_HOSTS", "valid.example.com,bad/host")
    with pytest.raises(ValueError, match="hosts"):
        mcp_server._allowed_hosts()


def test_mcp_public_url_requires_tls_off_loopback(monkeypatch):
    monkeypatch.delenv("DIFFUSE_ALLOW_PLAINTEXT_ORIGINS", raising=False)
    monkeypatch.setenv("DIFFUSE_PUBLIC_URL", "http://diffuse.example.com")
    with pytest.raises(ValueError, match="must use https"):
        mcp_server._public_url()

    monkeypatch.setenv("DIFFUSE_PUBLIC_URL", "http://localhost:8000")
    assert mcp_server._public_url() == "http://localhost:8000"
