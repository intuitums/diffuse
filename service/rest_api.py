"""Versioned, repository-authorized REST control-plane reads."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from contextlib import closing, suppress
from functools import partial
from typing import Annotated, Literal

import anyio
import httpx
import psycopg2
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    Security,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from indexer.store import get_conn
from service.analytics_store import get_review_analytics
from service.api_auth import AuthenticatedPrincipal, authenticate_bearer_token
from service.api_idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyReservation,
    complete_idempotency_key,
    idempotency_key_sha256,
    release_idempotency_lease,
    reserve_idempotency_key,
    save_idempotency_operation_data,
)
from service.api_tokens import (
    ADMIN_SCOPE,
    API_GENERATE_SCOPE,
    API_READ_SCOPE,
    API_WRITE_SCOPE,
)
from service.code_query import (
    ask_codebase,
    resolve_code_query_target,
    search_codebase,
)
from service.github import fetch_manual_pull_request_event
from service.gitlab import fetch_manual_gitlab_merge_request_event
from service.mcp_actions import enqueue_review_trigger
from service.mcp_store import (
    ProjectionNotFoundError,
    PullRequestState,
    ReviewStatus,
    get_mcp_code_review,
    get_mcp_merge_request,
    get_mcp_repository,
    get_mcp_review_trigger_target,
    list_mcp_code_reviews,
    list_mcp_merge_request_comments,
    list_mcp_merge_requests,
    list_mcp_repositories,
)
from service.repositories import (
    RegisteredRepository,
    update_mirror_state,
)
from service.repository_actions import (
    RepositoryAccessNotFoundError,
    RepositoryConfigurationConflictError,
    enqueue_repository_index_trigger,
    get_repository_for_index_action,
    register_repository_for_api,
)
from service.repository_indexing import repository_index_event
from service.repository_mirror import RepositoryMirror, RepositoryMirrorError
from service.review_trigger import fetch_current_manual_review_event
from service.scm import PullRequestEvent, PushEvent
from service.workflow import (
    DeliveryConflictError,
    EventOrderConflictError,
    RepositoryNotOnboardedError,
)

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"
MAX_API_PAGE_SIZE = 100
MAX_API_OFFSET = 1_000_000
REVIEW_TRIGGER_OPERATION = "review.trigger.v1"
REPOSITORY_CREATE_OPERATION = "repository.create.v1"
REPOSITORY_INDEX_OPERATION = "repository.index.v1"
REPOSITORY_INDEX_LEASE_SECONDS = 1800

router = APIRouter(prefix=API_PREFIX, tags=["Diffuse REST API v1"])
bearer = HTTPBearer(
    auto_error=False,
    scheme_name="DiffuseServiceToken",
    description=(
        "Diffuse bootstrap credential or non-recoverable service token. "
        "Read routes require diffuse:api:read; model-backed Q&A also requires "
        "diffuse:api:generate."
    ),
)


class RestApiError(Exception):
    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers or {}
        self.errors = errors


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CodeSearchRequest(_StrictRequest):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    path: Annotated[str | None, Field(min_length=1, max_length=1024)] = None
    include_related: bool = Field(default=False, alias="includeRelated")
    limit: int = Field(default=8, ge=1, le=20)


class CodeQuestionRequest(_StrictRequest):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    path: Annotated[str | None, Field(min_length=1, max_length=1024)] = None
    include_related: bool = Field(default=False, alias="includeRelated")
    limit: int = Field(default=8, ge=1, le=12)


class ReviewTriggerRequest(_StrictRequest):
    branch: Annotated[str | None, Field(min_length=1, max_length=255)] = None


class RepositoryCreateRequest(_StrictRequest):
    remote: Literal["github", "gitlab"]
    remote_url: Annotated[str, Field(alias="remoteUrl", min_length=8, max_length=2048)]
    name: Annotated[str, Field(min_length=3, max_length=512)]
    default_branch: Annotated[
        str,
        Field(alias="defaultBranch", min_length=1, max_length=255),
    ]


class RepositoryIndexRequest(_StrictRequest):
    pass


def _problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    errors: list[dict[str, object]] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"urn:diffuse:problem:{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
        media_type="application/problem+json",
    )


async def rest_api_error_handler(
    request: Request,
    error: RestApiError,
) -> JSONResponse:
    return _problem(
        request,
        status_code=error.status_code,
        code=error.code,
        title=error.title,
        detail=error.detail,
        headers=error.headers,
        errors=error.errors,
    )


async def rest_validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    safe_errors = [
        {
            "location": [
                str(part)
                for part in item.get("loc", ())
                if part not in {"body"}
            ],
            "message": item.get("msg", "Invalid value"),
            "type": item.get("type", "validation_error"),
        }
        for item in error.errors()
    ]
    return _problem(
        request,
        status_code=422,
        code="validation_failed",
        title="Request validation failed",
        detail="One or more request values are invalid.",
        errors=safe_errors,
    )


async def _authenticate(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer),
    ],
) -> AuthenticatedPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise RestApiError(
            401,
            code="authentication_required",
            title="Authentication required",
            detail="A valid Diffuse bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await authenticate_bearer_token(credentials.credentials)
    if principal is None:
        raise RestApiError(
            401,
            code="invalid_token",
            title="Authentication failed",
            detail="The Diffuse bearer token is invalid or inactive.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return principal


def require_api_scopes(*required: str):
    async def dependency(
        principal: Annotated[AuthenticatedPrincipal, Depends(_authenticate)],
    ) -> AuthenticatedPrincipal:
        if not principal.has_scopes(*required):
            scope = " ".join(required)
            raise RestApiError(
                403,
                code="insufficient_scope",
                title="Insufficient scope",
                detail="The service token does not grant the required API scope.",
                headers={
                    "WWW-Authenticate": (
                        f'Bearer error="insufficient_scope", scope="{scope}"'
                    )
                },
            )
        return principal

    return dependency


ReadPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_api_scopes(API_READ_SCOPE)),
]
GeneratePrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_api_scopes(API_READ_SCOPE, API_GENERATE_SCOPE)),
]
WritePrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(require_api_scopes(API_READ_SCOPE, API_WRITE_SCOPE)),
]


async def _require_repository_admin(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_api_scopes(ADMIN_SCOPE)),
    ],
) -> AuthenticatedPrincipal:
    if principal.authorized_repository_ids() is not None:
        raise RestApiError(
            403,
            code="all_repositories_required",
            title="All-repositories access required",
            detail=(
                "Repository onboarding requires an administrator token with "
                "all-repositories access."
            ),
        )
    return principal


RepositoryAdminPrincipal = Annotated[
    AuthenticatedPrincipal,
    Depends(_require_repository_admin),
]


def _run_database_query[T](
    principal: AuthenticatedPrincipal,
    callback: Callable[..., T],
    kwargs: dict[str, object],
) -> T:
    with closing(get_conn()) as conn:
        return callback(
            conn,
            authorized_repository_ids=principal.authorized_repository_ids(),
            **kwargs,
        )


async def _database_query[T](
    principal: AuthenticatedPrincipal,
    callback: Callable[..., T],
    **kwargs,
) -> T:
    try:
        return await anyio.to_thread.run_sync(
            partial(_run_database_query, principal, callback, kwargs)
        )
    except ProjectionNotFoundError:
        raise RestApiError(
            404,
            code="not_found",
            title="Resource not found",
            detail="The resource does not exist or is not authorized.",
        ) from None
    except RepositoryAccessNotFoundError:
        raise RestApiError(
            404,
            code="not_found",
            title="Resource not found",
            detail="The resource does not exist or is not authorized.",
        ) from None
    except RepositoryConfigurationConflictError as error:
        raise RestApiError(
            409,
            code="repository_conflict",
            title="Repository configuration conflict",
            detail=str(error),
        ) from None
    except ValueError as error:
        raise RestApiError(
            400,
            code="invalid_request",
            title="Invalid request",
            detail=str(error),
        ) from None
    except psycopg2.Error:
        raise RestApiError(
            503,
            code="data_store_unavailable",
            title="Data store unavailable",
            detail="Diffuse could not read its durable data store.",
        ) from None


def _run_database_mutation[T](
    principal: AuthenticatedPrincipal,
    callback: Callable[..., T],
    kwargs: dict[str, object],
) -> T:
    with closing(get_conn()) as conn, conn:
        return callback(
            conn,
            authorized_repository_ids=principal.authorized_repository_ids(),
            **kwargs,
        )


async def _database_mutation[T](
    principal: AuthenticatedPrincipal,
    callback: Callable[..., T],
    **kwargs,
) -> T:
    try:
        return await anyio.to_thread.run_sync(
            partial(_run_database_mutation, principal, callback, kwargs)
        )
    except ProjectionNotFoundError:
        raise RestApiError(
            404,
            code="not_found",
            title="Resource not found",
            detail="The resource does not exist or is not authorized.",
        ) from None
    except RepositoryAccessNotFoundError:
        raise RestApiError(
            404,
            code="not_found",
            title="Resource not found",
            detail="The resource does not exist or is not authorized.",
        ) from None
    except RepositoryConfigurationConflictError as error:
        raise RestApiError(
            409,
            code="repository_conflict",
            title="Repository configuration conflict",
            detail=str(error),
        ) from None
    except IdempotencyConflictError:
        raise RestApiError(
            409,
            code="idempotency_conflict",
            title="Idempotency conflict",
            detail="The Idempotency-Key was already used for another request.",
        ) from None
    except IdempotencyInProgressError:
        raise RestApiError(
            409,
            code="idempotency_in_progress",
            title="Request already in progress",
            detail="An identical API request is still in progress.",
            headers={"Retry-After": "5"},
        ) from None
    except ValueError as error:
        raise RestApiError(
            400,
            code="invalid_request",
            title="Invalid request",
            detail=str(error),
        ) from None
    except psycopg2.Error:
        raise RestApiError(
            503,
            code="data_store_unavailable",
            title="Data store unavailable",
            detail="Diffuse could not update its durable data store.",
        ) from None


def _versioned(payload: dict[str, object]) -> dict[str, object]:
    return {"apiVersion": API_VERSION, **payload}


@router.get("/repositories", summary="List repositories and index status")
async def list_repositories(
    principal: ReadPrincipal,
    enabled: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_API_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_API_OFFSET)] = 0,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        list_mcp_repositories,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )
    return _versioned(result)


def _repository_operation_request_sha256(
    operation: str,
    payload: dict[str, object],
) -> str:
    serialized = json.dumps(
        {"operation": operation, **payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _repository_action_actor(
    principal: AuthenticatedPrincipal,
) -> tuple[Literal["operator", "service_token"], int | None]:
    if principal.auth_kind == "bootstrap":
        return "operator", None
    if principal.auth_kind == "service_token" and principal.token_id is not None:
        return "service_token", principal.token_id
    raise RuntimeError("REST repository authorization context is invalid")


def _prepare_repository_create(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    request: RepositoryCreateRequest,
    actor_identity: str,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
    key_sha256: str,
) -> tuple[RegisteredRepository | None, IdempotencyReservation]:
    if authorized_repository_ids is not None:
        raise RepositoryAccessNotFoundError(
            "Repository onboarding requires all-repositories access"
        )
    reservation = reserve_idempotency_key(
        conn,
        actor_identity=actor_identity,
        operation=REPOSITORY_CREATE_OPERATION,
        key_sha256=key_sha256,
        request_sha256=_repository_operation_request_sha256(
            REPOSITORY_CREATE_OPERATION,
            {
                "remote": request.remote,
                "remote_url": request.remote_url,
                "name": request.name,
                "default_branch": request.default_branch,
            },
        ),
        lease_seconds=REPOSITORY_INDEX_LEASE_SECONDS,
    )
    if reservation.response is not None:
        return None, reservation
    repository, _created = register_repository_for_api(
        conn,
        scm_provider=request.remote,
        scm_base_url=request.remote_url,
        full_name=request.name,
        default_branch=request.default_branch,
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )
    return repository, reservation


def _prepare_repository_index(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    actor_identity: str,
    key_sha256: str,
) -> tuple[RegisteredRepository, IdempotencyReservation]:
    repository = get_repository_for_index_action(
        conn,
        repository_id=repository_id,
        authorized_repository_ids=authorized_repository_ids,
    )
    reservation = reserve_idempotency_key(
        conn,
        actor_identity=actor_identity,
        operation=REPOSITORY_INDEX_OPERATION,
        key_sha256=key_sha256,
        request_sha256=_repository_operation_request_sha256(
            REPOSITORY_INDEX_OPERATION,
            {"repository_id": repository_id},
        ),
        lease_seconds=REPOSITORY_INDEX_LEASE_SECONDS,
    )
    return repository, reservation


def _authorize_repository_operation(
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
) -> None:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise RepositoryAccessNotFoundError(
            "Repository does not exist or is not authorized"
        )


def _set_repository_index_syncing(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
) -> None:
    get_repository_for_index_action(
        conn,
        repository_id=repository_id,
        authorized_repository_ids=authorized_repository_ids,
    )
    update_mirror_state(conn, repository_id, state="syncing")


def _save_repository_index_event(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository: RegisteredRepository,
    reservation_id: int,
    actor_identity: str,
    event: PushEvent,
) -> None:
    current = get_repository_for_index_action(
        conn,
        repository_id=repository.id,
        authorized_repository_ids=authorized_repository_ids,
    )
    if (
        current.scm_provider != event.provider
        or current.scm_base_url != event.scm_base_url
        or current.full_name != event.repo_full_name
        or current.default_branch != event.default_branch
    ):
        raise RepositoryConfigurationConflictError(
            "Index request no longer matches repository configuration"
        )
    update_mirror_state(
        conn,
        repository.id,
        state="ready",
        commit_sha=event.after_sha,
    )
    save_idempotency_operation_data(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
        operation_data=event.to_payload(),
    )


def _complete_repository_operation(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
    response: dict[str, object],
) -> dict[str, object]:
    _authorize_repository_operation(
        authorized_repository_ids=authorized_repository_ids,
        repository_id=repository_id,
    )
    return complete_idempotency_key(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
        response=response,
    )


def _release_repository_operation(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
) -> None:
    _authorize_repository_operation(
        authorized_repository_ids=authorized_repository_ids,
        repository_id=repository_id,
    )
    release_idempotency_lease(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
    )


def _fail_repository_index_operation(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
) -> None:
    _authorize_repository_operation(
        authorized_repository_ids=authorized_repository_ids,
        repository_id=repository_id,
    )
    update_mirror_state(
        conn,
        repository_id,
        state="failed",
        error_code="mirror_sync_failed",
    )
    release_idempotency_lease(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
    )


async def _release_repository_operation_safely(
    principal: AuthenticatedPrincipal,
    *,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
) -> None:
    with suppress(RestApiError):
        await _database_mutation(
            principal,
            _release_repository_operation,
            repository_id=repository_id,
            reservation_id=reservation_id,
            actor_identity=actor_identity,
        )


async def _execute_repository_index_operation(
    principal: AuthenticatedPrincipal,
    *,
    repository: RegisteredRepository,
    reservation: IdempotencyReservation,
    operation: str,
    key_sha256: str,
) -> tuple[dict[str, object], bool]:
    if reservation.response is not None:
        return reservation.response, True

    actor_identity = _actor_identity(principal)
    try:
        if reservation.operation_data is not None:
            event = PushEvent.from_payload(reservation.operation_data)
        else:
            await _database_mutation(
                principal,
                _set_repository_index_syncing,
                repository_id=repository.id,
            )
            try:
                commit_sha = await anyio.to_thread.run_sync(
                    RepositoryMirror(repository).resolve_default_commit
                )
            except (
                OSError,
                RepositoryMirrorError,
                subprocess.SubprocessError,
            ):
                with suppress(RestApiError):
                    await _database_mutation(
                        principal,
                        _fail_repository_index_operation,
                        repository_id=repository.id,
                        reservation_id=reservation.id,
                        actor_identity=actor_identity,
                    )
                raise RestApiError(
                    502,
                    code="repository_unavailable",
                    title="Repository unavailable",
                    detail=(
                        "Diffuse could not fetch and verify the repository's "
                        "current default-branch commit."
                    ),
                ) from None
            delivery_seed = hashlib.sha256(
                f"{operation}\0{actor_identity}\0{key_sha256}".encode()
            ).hexdigest()
            event = repository_index_event(
                repository,
                commit_sha=commit_sha,
                requested_at=reservation.requested_at,
                delivery_id=f"api-index-{delivery_seed[:48]}",
            )
            await _database_mutation(
                principal,
                _save_repository_index_event,
                repository=repository,
                reservation_id=reservation.id,
                actor_identity=actor_identity,
                event=event,
            )

        actor_kind, actor_token_id = _repository_action_actor(principal)
        result = await _database_mutation(
            principal,
            enqueue_repository_index_trigger,
            event=event,
            repository_id=repository.id,
            actor_kind=actor_kind,
            actor_label=principal.subject,
            actor_token_id=actor_token_id,
        )
        completed = await _database_mutation(
            principal,
            _complete_repository_operation,
            repository_id=repository.id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
            response=_versioned(result),
        )
        return completed, False
    except RestApiError:
        await _release_repository_operation_safely(
            principal,
            repository_id=repository.id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise
    except (
        DeliveryConflictError,
        EventOrderConflictError,
        RepositoryNotOnboardedError,
        RuntimeError,
        ValueError,
    ):
        await _release_repository_operation_safely(
            principal,
            repository_id=repository.id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise RestApiError(
            409,
            code="repository_index_conflict",
            title="Repository index conflict",
            detail=(
                "The index request conflicts with the repository's current "
                "configuration or durable event state."
            ),
        ) from None


@router.post(
    "/repositories",
    status_code=202,
    summary="Onboard a repository and queue its initial index",
)
async def create_repository(
    request: RepositoryCreateRequest,
    principal: RepositoryAdminPrincipal,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        ),
    ],
) -> dict[str, object]:
    actor_identity = _actor_identity(principal)
    actor_kind, actor_token_id = _repository_action_actor(principal)
    key_sha256 = idempotency_key_sha256(idempotency_key)
    repository, reservation = await _database_mutation(
        principal,
        _prepare_repository_create,
        request=request,
        actor_identity=actor_identity,
        actor_kind=actor_kind,
        actor_label=principal.subject,
        actor_token_id=actor_token_id,
        key_sha256=key_sha256,
    )
    if reservation.response is not None:
        result = reservation.response
        replayed = True
    else:
        if repository is None:
            raise RestApiError(
                500,
                code="internal_state_error",
                title="Internal state error",
                detail="Diffuse could not recover the repository onboarding state.",
            )
        result, replayed = await _execute_repository_index_operation(
            principal,
            repository=repository,
            reservation=reservation,
            operation=REPOSITORY_CREATE_OPERATION,
            key_sha256=key_sha256,
        )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    repository_payload = result.get("repository")
    if isinstance(repository_payload, dict) and isinstance(
        repository_payload.get("id"),
        int,
    ):
        response.headers["Location"] = (
            f"{API_PREFIX}/repositories/{repository_payload['id']}"
        )
    return result


@router.get(
    "/repositories/{repository_id}",
    summary="Get one repository and its active index",
)
async def get_repository(
    repository_id: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        get_mcp_repository,
        repository_id=repository_id,
    )
    return _versioned(result)


@router.post(
    "/repositories/{repository_id}/indexes",
    status_code=202,
    summary="Fetch and queue the current default-branch commit",
)
async def create_repository_index(
    repository_id: Annotated[int, Path(gt=0)],
    _request: RepositoryIndexRequest,
    principal: WritePrincipal,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        ),
    ],
) -> dict[str, object]:
    actor_identity = _actor_identity(principal)
    key_sha256 = idempotency_key_sha256(idempotency_key)
    repository, reservation = await _database_mutation(
        principal,
        _prepare_repository_index,
        repository_id=repository_id,
        actor_identity=actor_identity,
        key_sha256=key_sha256,
    )
    result, replayed = await _execute_repository_index_operation(
        principal,
        repository=repository,
        reservation=reservation,
        operation=REPOSITORY_INDEX_OPERATION,
        key_sha256=key_sha256,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result


@router.get(
    "/repositories/{repository_id}/pull-requests",
    summary="List repository pull or merge requests",
)
async def list_pull_requests(
    repository_id: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
    state: Annotated[PullRequestState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_API_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_API_OFFSET)] = 0,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        list_mcp_merge_requests,
        repository_id=repository_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return _versioned(result)


@router.get(
    "/repositories/{repository_id}/pull-requests/{pull_request_number}",
    summary="Get pull-request review activity and findings",
)
async def get_pull_request(
    repository_id: Annotated[int, Path(gt=0)],
    pull_request_number: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        get_mcp_merge_request,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
    )
    return _versioned(result)


def _actor_identity(principal: AuthenticatedPrincipal) -> str:
    if principal.auth_kind == "bootstrap":
        return "operator:self-hosted-operator"
    if principal.auth_kind == "service_token" and principal.token_id is not None:
        return f"service_token:{principal.token_id}"
    raise RuntimeError("REST write authorization context is invalid")


def _review_trigger_request_sha256(
    *,
    repository_id: int,
    pull_request_number: int,
    branch: str | None,
) -> str:
    payload = json.dumps(
        {
            "operation": REVIEW_TRIGGER_OPERATION,
            "repository_id": repository_id,
            "pull_request_number": pull_request_number,
            "branch": branch,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _prepare_review_trigger(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    pull_request_number: int,
    branch: str | None,
    actor_identity: str,
    key_sha256: str,
) -> tuple[dict[str, object], IdempotencyReservation]:
    target = get_mcp_review_trigger_target(
        conn,
        authorized_repository_ids=authorized_repository_ids,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
    )
    if branch is not None and branch != target["headBranch"]:
        raise ValueError("Requested branch does not match the pull-request head")
    reservation = reserve_idempotency_key(
        conn,
        actor_identity=actor_identity,
        operation=REVIEW_TRIGGER_OPERATION,
        key_sha256=key_sha256,
        request_sha256=_review_trigger_request_sha256(
            repository_id=repository_id,
            pull_request_number=pull_request_number,
            branch=branch,
        ),
    )
    return target, reservation


def _save_review_trigger_event(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
    event: PullRequestEvent,
) -> None:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise ProjectionNotFoundError(
            "Repository does not exist or is not authorized"
        )
    save_idempotency_operation_data(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
        operation_data=event.to_payload(),
    )


def _complete_review_trigger(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
    response: dict[str, object],
) -> dict[str, object]:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise ProjectionNotFoundError(
            "Repository does not exist or is not authorized"
        )
    return complete_idempotency_key(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
        response=response,
    )


def _release_review_trigger(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
) -> None:
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        return
    release_idempotency_lease(
        conn,
        reservation_id=reservation_id,
        actor_identity=actor_identity,
    )


async def _release_review_trigger_safely(
    principal: AuthenticatedPrincipal,
    *,
    repository_id: int,
    reservation_id: int,
    actor_identity: str,
) -> None:
    with suppress(RestApiError):
        await _database_mutation(
            principal,
            _release_review_trigger,
            repository_id=repository_id,
            reservation_id=reservation_id,
            actor_identity=actor_identity,
        )


@router.post(
    "/repositories/{repository_id}/pull-requests/{pull_request_number}/reviews",
    status_code=202,
    summary="Request an authoritative pull-request review",
)
async def trigger_pull_request_review(
    repository_id: Annotated[int, Path(gt=0)],
    pull_request_number: Annotated[int, Path(gt=0)],
    request: ReviewTriggerRequest,
    principal: WritePrincipal,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        ),
    ],
) -> dict[str, object]:
    actor_identity = _actor_identity(principal)
    key_sha256 = idempotency_key_sha256(idempotency_key)
    target, reservation = await _database_mutation(
        principal,
        _prepare_review_trigger,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
        branch=request.branch,
        actor_identity=actor_identity,
        key_sha256=key_sha256,
    )
    if reservation.response is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return reservation.response

    try:
        if reservation.operation_data is not None:
            event = PullRequestEvent.from_payload(reservation.operation_data)
        else:
            trigger_key = hashlib.sha256(
                f"{actor_identity}\0{key_sha256}".encode()
            ).hexdigest()[:40]
            try:
                event = await fetch_current_manual_review_event(
                    target=target,
                    pull_request_number=pull_request_number,
                    requested_by=principal.subject,
                    requested_at=reservation.requested_at.isoformat(),
                    trigger_source="api",
                    trigger_key=trigger_key,
                    branch=request.branch,
                    github_fetch=fetch_manual_pull_request_event,
                    gitlab_fetch=fetch_manual_gitlab_merge_request_event,
                )
            except (HTTPException, httpx.HTTPError, RuntimeError):
                await _release_review_trigger_safely(
                    principal,
                    repository_id=repository_id,
                    reservation_id=reservation.id,
                    actor_identity=actor_identity,
                )
                raise RestApiError(
                    502,
                    code="provider_unavailable",
                    title="Source provider unavailable",
                    detail=(
                        "Diffuse could not verify the current pull-request state."
                    ),
                ) from None
            await _database_mutation(
                principal,
                _save_review_trigger_event,
                repository_id=repository_id,
                reservation_id=reservation.id,
                actor_identity=actor_identity,
                event=event,
            )
        actor_kind = (
            "operator"
            if principal.auth_kind == "bootstrap"
            else "service_token"
        )
        result = await _database_mutation(
            principal,
            enqueue_review_trigger,
            event=event,
            repository_id=repository_id,
            actor_kind=actor_kind,
            actor_label=principal.subject,
            actor_token_id=principal.token_id,
        )
        completed = await _database_mutation(
            principal,
            _complete_review_trigger,
            repository_id=repository_id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
            response=_versioned(result),
        )
        return completed
    except RestApiError:
        await _release_review_trigger_safely(
            principal,
            repository_id=repository_id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise
    except ValueError:
        await _release_review_trigger_safely(
            principal,
            repository_id=repository_id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise RestApiError(
            409,
            code="review_not_triggerable",
            title="Review cannot be triggered",
            detail="The pull request is no longer open at the requested head.",
        ) from None
    except (DeliveryConflictError, EventOrderConflictError):
        await _release_review_trigger_safely(
            principal,
            repository_id=repository_id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise RestApiError(
            409,
            code="review_trigger_conflict",
            title="Review trigger conflict",
            detail="The provider event conflicts with durable pull-request state.",
        ) from None
    except RuntimeError:
        await _release_review_trigger_safely(
            principal,
            repository_id=repository_id,
            reservation_id=reservation.id,
            actor_identity=actor_identity,
        )
        raise RestApiError(
            500,
            code="internal_state_error",
            title="Internal state error",
            detail="Diffuse could not complete the durable review trigger.",
        ) from None


@router.get(
    "/repositories/{repository_id}/pull-requests/{pull_request_number}/reviews",
    summary="List reviews for one pull request",
)
async def list_pull_request_reviews(
    repository_id: Annotated[int, Path(gt=0)],
    pull_request_number: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
    status_filter: Annotated[
        ReviewStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_API_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_API_OFFSET)] = 0,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        list_mcp_code_reviews,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return _versioned(result)


@router.get(
    "/repositories/{repository_id}/pull-requests/{pull_request_number}/findings",
    summary="List current findings for one pull request",
)
async def list_pull_request_findings(
    repository_id: Annotated[int, Path(gt=0)],
    pull_request_number: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
    addressed: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_API_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_API_OFFSET)] = 0,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        list_mcp_merge_request_comments,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
        addressed=addressed,
        limit=limit,
        offset=offset,
    )
    return _versioned(result)


@router.get("/reviews", summary="List code reviews")
async def list_reviews(
    principal: ReadPrincipal,
    repository_id: Annotated[int | None, Query(gt=0)] = None,
    pull_request_number: Annotated[int | None, Query(gt=0)] = None,
    status_filter: Annotated[
        ReviewStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_API_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_API_OFFSET)] = 0,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        list_mcp_code_reviews,
        repository_id=repository_id,
        pull_request_number=pull_request_number,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return _versioned(result)


@router.get("/reviews/{review_id}", summary="Get a review and its findings")
async def get_review(
    review_id: Annotated[int, Path(gt=0)],
    principal: ReadPrincipal,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        get_mcp_code_review,
        code_review_id=f"review_{review_id}",
    )
    return _versioned(result)


@router.get("/analytics/reviews", summary="Get review analytics")
async def review_analytics(
    principal: ReadPrincipal,
    start_at: Annotated[str, Query(alias="startAt", min_length=20, max_length=64)],
    end_at: Annotated[str, Query(alias="endAt", min_length=20, max_length=64)],
    repository_id: Annotated[int | None, Query(gt=0)] = None,
    author: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        get_review_analytics,
        start_at=start_at,
        end_at=end_at,
        repository_id=repository_id,
        author=author,
    )
    return _versioned(result)


def _run_code_search(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    request: CodeSearchRequest,
) -> dict[str, object]:
    target = resolve_code_query_target(
        conn,
        repository_id=repository_id,
        include_related=request.include_related,
        authorized_repository_ids=authorized_repository_ids,
    )
    return search_codebase(
        target,
        query=request.query,
        path_prefix=request.path,
        limit=request.limit,
    )


@router.post(
    "/repositories/{repository_id}/code/search",
    summary="Search an immutable code index",
)
async def search_repository_code(
    repository_id: Annotated[int, Path(gt=0)],
    request: CodeSearchRequest,
    principal: ReadPrincipal,
) -> dict[str, object]:
    result = await _database_query(
        principal,
        _run_code_search,
        repository_id=repository_id,
        request=request,
    )
    return _versioned(result)


def _run_code_question(
    conn,
    *,
    authorized_repository_ids: frozenset[int] | None,
    repository_id: int,
    request: CodeQuestionRequest,
) -> dict[str, object]:
    target = resolve_code_query_target(
        conn,
        repository_id=repository_id,
        include_related=request.include_related,
        authorized_repository_ids=authorized_repository_ids,
    )
    return ask_codebase(
        target,
        question=request.question,
        path_prefix=request.path,
        limit=request.limit,
    )


@router.post(
    "/repositories/{repository_id}/code/ask",
    summary="Answer a repository question with exact citations",
)
async def ask_repository_code(
    repository_id: Annotated[int, Path(gt=0)],
    request: CodeQuestionRequest,
    principal: GeneratePrincipal,
) -> dict[str, object]:
    try:
        result = await _database_query(
            principal,
            _run_code_question,
            repository_id=repository_id,
            request=request,
        )
    except RuntimeError:
        raise RestApiError(
            502,
            code="generation_failed",
            title="Generation failed",
            detail="Diffuse could not produce a grounded repository answer.",
        ) from None
    return _versioned(result)
