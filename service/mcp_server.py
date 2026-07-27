"""Bearer-authenticated Model Context Protocol surface for Diffuse."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import anyio
import psycopg2
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AliasChoices, AnyHttpUrl, Field

from indexer.store import get_conn
from service.analytics_store import (
    get_review_analytics as query_review_analytics,
)
from service.api_auth import authenticate_bearer_token
from service.api_tokens import (
    ADMIN_SCOPE,
    MCP_GENERATE_SCOPE,
    MCP_READ_SCOPE,
    MCP_WRITE_SCOPE,
)
from service.code_query import (
    ask_codebase as answer_codebase_query,
)
from service.code_query import (
    resolve_code_query_target,
    search_codebase,
)
from service.custom_context_store import (
    CustomContextStatus,
    CustomContextType,
)
from service.custom_context_store import (
    create_custom_context as create_custom_context_record,
)
from service.custom_context_store import (
    delete_custom_context as delete_custom_context_record,
)
from service.custom_context_store import (
    update_custom_context as update_custom_context_record,
)
from service.github import fetch_manual_pull_request_event
from service.gitlab import fetch_manual_gitlab_merge_request_event
from service.mcp_actions import enqueue_mcp_review_trigger
from service.mcp_store import (
    AgentTarget,
    McpCustomContextStatus,
    McpCustomContextType,
    McpRemote,
    PullRequestState,
    ReviewStatus,
    get_mcp_code_review,
    get_mcp_custom_context,
    get_mcp_fix_all_handoff,
    get_mcp_fix_handoff,
    get_mcp_merge_request,
    get_mcp_review_trigger_target,
    list_mcp_code_reviews,
    list_mcp_custom_context,
    list_mcp_merge_request_comments,
    list_mcp_merge_requests,
    list_mcp_repositories,
    search_mcp_custom_context,
    search_mcp_review_comments,
)
from service.review_trigger import (
    fetch_current_manual_review_event,
)
from service.scm import PLAINTEXT_ORIGIN_VARIABLE, plaintext_origin_allowed

DEFAULT_ALLOWED_HOSTS = (
    "localhost:*",
    "127.0.0.1:*",
    "[::1]:*",
    "testserver",
)


def _public_url() -> str:
    value = os.environ.get("DIFFUSE_PUBLIC_URL", "http://localhost:8000").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "DIFFUSE_PUBLIC_URL must be an HTTP(S) origin without credentials or a path"
        )
    # This origin is advertised to MCP clients, which then send their bearer
    # token to it, so it is credential-bearing in exactly the same way an SCM
    # base URL is.
    if parsed.scheme == "http" and not plaintext_origin_allowed(parsed.hostname):
        raise ValueError(
            "DIFFUSE_PUBLIC_URL must use https; plaintext is accepted only for "
            f"loopback or when {PLAINTEXT_ORIGIN_VARIABLE}=1"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _allowed_hosts() -> list[str]:
    raw = os.environ.get("DIFFUSE_MCP_ALLOWED_HOSTS")
    values = (
        [part.strip() for part in raw.split(",")]
        if raw is not None
        else list(DEFAULT_ALLOWED_HOSTS)
    )
    if (
        not values
        or len(values) > 32
        or any(
            not value
            or len(value) > 255
            or not value.isascii()
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "/" in value
            for value in values
        )
    ):
        raise ValueError(
            "DIFFUSE_MCP_ALLOWED_HOSTS must contain 1 to 32 comma-separated hosts"
        )
    return values


class DiffuseTokenVerifier(TokenVerifier):
    """Validate the recovery credential or a durable scoped service token."""

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await authenticate_bearer_token(token)
        if principal is None:
            return None
        claims: dict[str, object] = {
            "auth_kind": principal.auth_kind,
            "all_repositories": principal.all_repositories,
            "repository_ids": list(principal.repository_ids),
        }
        if principal.token_id is not None:
            claims["token_id"] = principal.token_id
        return AccessToken(
            token=token,
            client_id=principal.client_id,
            scopes=list(principal.scopes),
            expires_at=(
                int(principal.expires_at.timestamp())
                if principal.expires_at is not None
                else None
            ),
            subject=principal.subject,
            claims=claims,
        )


def _authorized_repository_ids(
    access: AccessToken | None = None,
) -> frozenset[int] | None:
    access = access or get_access_token()
    claims = access.claims if access is not None else None
    if not isinstance(claims, dict):
        raise RuntimeError("MCP authorization context is unavailable")
    auth_kind = claims.get("auth_kind")
    all_repositories = claims.get("all_repositories")
    raw_repository_ids = claims.get("repository_ids")
    if (
        auth_kind not in {"bootstrap", "service_token"}
        or type(all_repositories) is not bool
    ):
        raise RuntimeError("MCP authorization context is invalid")
    if auth_kind == "bootstrap" and all_repositories is not True:
        raise RuntimeError("MCP authorization context is invalid")
    if auth_kind == "service_token" and (
        type(claims.get("token_id")) is not int or claims["token_id"] <= 0
    ):
        raise RuntimeError("MCP authorization context is invalid")
    if all_repositories is True:
        if raw_repository_ids != []:
            raise RuntimeError("MCP authorization context is invalid")
        return None
    if (
        not isinstance(raw_repository_ids, list)
        or not raw_repository_ids
        or len(raw_repository_ids) > 100
        or any(type(value) is not int or value <= 0 for value in raw_repository_ids)
    ):
        raise RuntimeError("MCP authorization context is invalid")
    return frozenset(raw_repository_ids)


@dataclass(frozen=True)
class McpWriteAuthorization:
    authorized_repository_ids: frozenset[int] | None
    actor_kind: Literal["operator", "service_token"]
    actor_label: str
    actor_token_id: int | None


def _mcp_write_authorization() -> McpWriteAuthorization:
    access = get_access_token()
    if access is None or not (
        MCP_WRITE_SCOPE in access.scopes or ADMIN_SCOPE in access.scopes
    ):
        raise RuntimeError("MCP write scope is required")
    claims = access.claims
    authorized_repository_ids = _authorized_repository_ids(access)
    if not isinstance(claims, dict):
        raise RuntimeError("MCP authorization context is unavailable")
    if claims["auth_kind"] == "bootstrap":
        return McpWriteAuthorization(
            authorized_repository_ids=authorized_repository_ids,
            actor_kind="operator",
            actor_label=access.subject or "self-hosted-operator",
            actor_token_id=None,
        )
    return McpWriteAuthorization(
        authorized_repository_ids=authorized_repository_ids,
        actor_kind="service_token",
        actor_label=access.subject or access.client_id,
        actor_token_id=int(claims["token_id"]),
    )


def _require_mcp_generation_scope() -> None:
    access = get_access_token()
    if access is None or not (
        MCP_GENERATE_SCOPE in access.scopes or ADMIN_SCOPE in access.scopes
    ):
        raise RuntimeError("MCP generation scope is required")


def _database_query[T](callback: Callable[..., T], **kwargs) -> T:
    authorized_repository_ids = _authorized_repository_ids()
    try:
        with closing(get_conn()) as conn:
            return callback(
                conn,
                authorized_repository_ids=authorized_repository_ids,
                **kwargs,
            )
    except psycopg2.Error:
        raise RuntimeError("Diffuse data store is unavailable") from None


def _database_write[T](callback: Callable[..., T], **kwargs) -> T:
    authorization = _mcp_write_authorization()
    try:
        with closing(get_conn()) as conn, conn:
            return callback(
                conn,
                authorized_repository_ids=(
                    authorization.authorized_repository_ids
                ),
                actor_kind=authorization.actor_kind,
                actor_label=authorization.actor_label,
                actor_token_id=authorization.actor_token_id,
                **kwargs,
            )
    except psycopg2.Error:
        raise RuntimeError("Diffuse data store is unavailable") from None


# FastMCP dispatches a synchronous tool inline on the event loop, and this MCP app shares
# that loop with the webhook app in a single uvicorn process, so blocking psycopg2 work in
# a tool stalls webhook deliveries and can time out the /ready healthcheck. Every tool
# therefore offloads its blocking work to a worker thread; anyio copies the current context
# into that thread, so the MCP access-token contextvar stays visible to the callbacks.
async def _database_query_async[T](callback: Callable[..., T], **kwargs) -> T:
    return await anyio.to_thread.run_sync(partial(_database_query, callback, **kwargs))


async def _database_write_async[T](callback: Callable[..., T], **kwargs) -> T:
    return await anyio.to_thread.run_sync(partial(_database_write, callback, **kwargs))


public_url = _public_url()
diffuse_mcp = FastMCP(
    "Diffuse",
    instructions=(
        "Inspect self-hosted Diffuse repositories, code reviews, current review "
        "findings, and custom context. Write tools require an explicit MCP write scope."
    ),
    token_verifier=DiffuseTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(f"{public_url}/mcp"),
        required_scopes=[MCP_READ_SCOPE],
    ),
    host="0.0.0.0",
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=[
            public_url,
            "http://localhost:*",
            "http://127.0.0.1:*",
            "http://[::1]:*",
        ],
    ),
)


@diffuse_mcp.tool()
async def list_repositories(
    enabled: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """List onboarded repositories and active immutable index snapshots."""
    return await _database_query_async(
        list_mcp_repositories,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def get_review_analytics(
    startAt: Annotated[
        str,
        Field(
            min_length=20,
            max_length=64,
            validation_alias=AliasChoices("startAt", "start_at"),
        ),
    ],
    endAt: Annotated[
        str,
        Field(
            min_length=20,
            max_length=64,
            validation_alias=AliasChoices("endAt", "end_at"),
        ),
    ],
    name: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ] = None,
    remote: McpRemote | None = None,
    defaultBranch: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ] = None,
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    author: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
) -> dict[str, object]:
    """Report exact review, finding, engagement, and usage metrics."""
    return await _database_query_async(
        query_review_analytics,
        start_at=startAt,
        end_at=endAt,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        author=author,
    )


@diffuse_mcp.tool()
async def search_code(
    name: Annotated[
        str,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ],
    remote: McpRemote,
    defaultBranch: Annotated[
        str,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ],
    query: Annotated[str, Field(min_length=1, max_length=2000)],
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    path: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
    includeRelated: Annotated[
        bool,
        Field(
            validation_alias=AliasChoices(
                "includeRelated",
                "include_related",
            )
        ),
    ] = False,
    limit: Annotated[int, Field(ge=1, le=20)] = 8,
) -> dict[str, object]:
    """Search one authorized immutable code index with source permalinks."""
    target = await _database_query_async(
        resolve_code_query_target,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        include_related=includeRelated,
    )
    return await anyio.to_thread.run_sync(
        partial(
            search_codebase,
            target,
            query=query,
            path_prefix=path,
            limit=limit,
        )
    )


@diffuse_mcp.tool()
async def ask_codebase(
    name: Annotated[
        str,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ],
    remote: McpRemote,
    defaultBranch: Annotated[
        str,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ],
    question: Annotated[str, Field(min_length=1, max_length=2000)],
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    path: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
    includeRelated: Annotated[
        bool,
        Field(
            validation_alias=AliasChoices(
                "includeRelated",
                "include_related",
            )
        ),
    ] = False,
    limit: Annotated[int, Field(ge=1, le=12)] = 8,
) -> dict[str, object]:
    """Answer a repository question using only exact retrieved source ranges."""
    _require_mcp_generation_scope()
    target = await _database_query_async(
        resolve_code_query_target,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        include_related=includeRelated,
    )
    return await anyio.to_thread.run_sync(
        partial(
            answer_codebase_query,
            target,
            question=question,
            path_prefix=path,
            limit=limit,
        )
    )


@diffuse_mcp.tool()
async def list_code_reviews(
    name: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ] = None,
    remote: McpRemote | None = None,
    defaultBranch: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ] = None,
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    prNumber: Annotated[
        int | None,
        Field(validation_alias=AliasChoices("prNumber", "pull_request_number")),
    ] = None,
    status: ReviewStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """List durable reviews, optionally filtered by repository, PR, or status."""
    return await _database_query_async(
        list_mcp_code_reviews,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        pull_request_number=prNumber,
        status=status,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def list_merge_requests(
    name: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ] = None,
    remote: McpRemote | None = None,
    defaultBranch: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ] = None,
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    state: PullRequestState | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """List durable pull/merge requests and their review activity."""
    return await _database_query_async(
        list_mcp_merge_requests,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        state=state,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def list_pull_requests(
    name: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ] = None,
    remote: McpRemote | None = None,
    defaultBranch: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ] = None,
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    state: PullRequestState | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """GitHub-compatible alias for list_merge_requests."""
    return await _database_query_async(
        list_mcp_merge_requests,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        state=state,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def get_merge_request(
    name: Annotated[
        str,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ],
    remote: McpRemote,
    defaultBranch: Annotated[
        str,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ],
    prNumber: Annotated[
        int,
        Field(validation_alias=AliasChoices("prNumber", "pull_request_number")),
    ],
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
) -> dict[str, object]:
    """Get pull-request metadata, Diffuse findings, and review history."""
    return await _database_query_async(
        get_mcp_merge_request,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        pull_request_number=prNumber,
    )


@diffuse_mcp.tool()
async def trigger_code_review(
    name: Annotated[
        str,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ],
    remote: McpRemote,
    defaultBranch: Annotated[
        str,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ],
    prNumber: Annotated[
        int,
        Field(validation_alias=AliasChoices("prNumber", "pull_request_number")),
    ],
    branch: str | None = None,
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
) -> dict[str, object]:
    """Fetch current provider PR/MR state and queue an audited manual review."""
    authorization = _mcp_write_authorization()
    target = await _database_query_async(
        get_mcp_review_trigger_target,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        pull_request_number=prNumber,
    )
    trigger_key = uuid4().hex
    event = await fetch_current_manual_review_event(
        target=target,
        pull_request_number=prNumber,
        requested_by=authorization.actor_label,
        requested_at=datetime.now(UTC).isoformat(),
        trigger_source="mcp",
        trigger_key=trigger_key,
        branch=branch,
        github_fetch=fetch_manual_pull_request_event,
        gitlab_fetch=fetch_manual_gitlab_merge_request_event,
    )
    return await _database_write_async(
        enqueue_mcp_review_trigger,
        event=event,
        repository_id=int(target["repositoryId"]),
    )


@diffuse_mcp.tool()
async def get_code_review(
    codeReviewId: Annotated[
        str,
        Field(validation_alias=AliasChoices("codeReviewId", "code_review_id")),
    ],
) -> dict[str, object]:
    """Get one review's report, provenance, coverage, and structured findings."""
    return await _database_query_async(
        get_mcp_code_review,
        code_review_id=codeReviewId,
    )


@diffuse_mcp.tool()
async def get_fix_handoff(
    codeReviewId: Annotated[
        str,
        Field(validation_alias=AliasChoices("codeReviewId", "code_review_id")),
    ],
    findingFingerprint: Annotated[
        str,
        Field(
            validation_alias=AliasChoices(
                "findingFingerprint",
                "finding_fingerprint",
            )
        ),
    ],
    agent: AgentTarget = "mcp",
) -> dict[str, object]:
    """Build a revision-safe handoff for one current finding."""
    return await _database_query_async(
        get_mcp_fix_handoff,
        code_review_id=codeReviewId,
        finding_fingerprint=findingFingerprint,
        agent=agent,
    )


@diffuse_mcp.tool()
async def get_fix_all_handoff(
    codeReviewId: Annotated[
        str,
        Field(validation_alias=AliasChoices("codeReviewId", "code_review_id")),
    ],
    agent: AgentTarget = "mcp",
) -> dict[str, object]:
    """Build a revision-safe handoff for every current finding in a review."""
    return await _database_query_async(
        get_mcp_fix_all_handoff,
        code_review_id=codeReviewId,
        agent=agent,
    )


@diffuse_mcp.tool()
async def list_merge_request_comments(
    name: Annotated[
        str,
        Field(validation_alias=AliasChoices("name", "repository_name")),
    ],
    remote: McpRemote,
    defaultBranch: Annotated[
        str,
        Field(validation_alias=AliasChoices("defaultBranch", "default_branch")),
    ],
    prNumber: Annotated[
        int,
        Field(validation_alias=AliasChoices("prNumber", "pull_request_number")),
    ],
    remoteUrl: Annotated[
        str | None,
        Field(validation_alias=AliasChoices("remoteUrl", "remote_url")),
    ] = None,
    diffuseGenerated: Annotated[
        bool | None,
        Field(validation_alias=AliasChoices("diffuseGenerated", "generated")),
    ] = None,
    addressed: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """List each current published Diffuse finding lineage for a pull request."""
    return await _database_query_async(
        list_mcp_merge_request_comments,
        repository_name=name,
        remote=remote,
        default_branch=defaultBranch,
        remote_url=remoteUrl,
        pull_request_number=prNumber,
        addressed=addressed,
        generated=diffuseGenerated,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def search_review_comments(
    query: str,
    repository_id: int | None = None,
    include_addressed: bool = False,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, object]:
    """Search current published findings by title, body, evidence, or file path."""
    return await _database_query_async(
        search_mcp_review_comments,
        query=query,
        repository_id=repository_id,
        include_addressed=include_addressed,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def list_custom_context(
    repository_id: int | None = None,
    status: McpCustomContextStatus | None = None,
    type: Annotated[
        McpCustomContextType | None,
        Field(validation_alias=AliasChoices("type", "context_type")),
    ] = None,
    diffuseGenerated: Annotated[
        bool | None,
        Field(validation_alias=AliasChoices("diffuseGenerated", "generated")),
    ] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """List inspectable feedback-derived rules and their repository scope."""
    return await _database_query_async(
        list_mcp_custom_context,
        repository_id=repository_id,
        status=status,
        context_type=type,
        generated=diffuseGenerated,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def get_custom_context(
    customContextId: Annotated[
        str,
        Field(
            validation_alias=AliasChoices(
                "customContextId",
                "custom_context_id",
            )
        ),
    ],
) -> dict[str, object]:
    """Get operator context or a learned rule with provenance history."""
    return await _database_query_async(
        get_mcp_custom_context,
        custom_context_id=customContextId,
    )


@diffuse_mcp.tool()
async def search_custom_context(
    query: str,
    repository_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, object]:
    """Search authorized custom context and learned rules by literal content."""
    return await _database_query_async(
        search_mcp_custom_context,
        query=query,
        repository_id=repository_id,
        limit=limit,
        offset=offset,
    )


@diffuse_mcp.tool()
async def create_custom_context(
    repository_id: int,
    body: str,
    applies_to: list[str],
    context_type: CustomContextType = "CUSTOM_INSTRUCTION",
    status: CustomContextStatus = "active",
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create audited repository-scoped context; requires diffuse:mcp:write."""
    return {
        "customContext": await _database_write_async(
            create_custom_context_record,
            repository_id=repository_id,
            context_type=context_type,
            body=body,
            applies_to=tuple(applies_to),
            status=status,
            metadata=metadata or {},
        )
    }


@diffuse_mcp.tool()
async def update_custom_context(
    customContextId: Annotated[
        str,
        Field(
            validation_alias=AliasChoices(
                "customContextId",
                "custom_context_id",
            )
        ),
    ],
    expectedUpdatedAt: Annotated[
        str,
        Field(
            min_length=20,
            max_length=64,
            validation_alias=AliasChoices(
                "expectedUpdatedAt",
                "expected_updated_at",
            ),
        ),
    ],
    body: Annotated[str | None, Field(min_length=1, max_length=12_000)] = None,
    appliesTo: Annotated[
        list[str] | None,
        Field(
            min_length=1,
            max_length=32,
            validation_alias=AliasChoices("appliesTo", "applies_to"),
        ),
    ] = None,
    type: Annotated[
        CustomContextType | None,
        Field(validation_alias=AliasChoices("type", "context_type")),
    ] = None,
    status: CustomContextStatus | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Edit operator context with optimistic concurrency and immutable audit."""
    return await _database_write_async(
        update_custom_context_record,
        custom_context_id=customContextId,
        expected_updated_at=expectedUpdatedAt,
        context_type=type,
        body=body,
        applies_to=tuple(appliesTo) if appliesTo is not None else None,
        status=status,
        metadata=metadata,
    )


@diffuse_mcp.tool()
async def delete_custom_context(
    customContextId: Annotated[
        str,
        Field(
            validation_alias=AliasChoices(
                "customContextId",
                "custom_context_id",
            )
        ),
    ],
    expectedUpdatedAt: Annotated[
        str,
        Field(
            min_length=20,
            max_length=64,
            validation_alias=AliasChoices(
                "expectedUpdatedAt",
                "expected_updated_at",
            ),
        ),
    ],
) -> dict[str, object]:
    """Permanently delete operator context while retaining an audit tombstone."""
    return await _database_write_async(
        delete_custom_context_record,
        custom_context_id=customContextId,
        expected_updated_at=expectedUpdatedAt,
    )


mcp_http_app = diffuse_mcp.streamable_http_app()
