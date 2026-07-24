from datetime import UTC, datetime

import httpx
import pytest

from service import rest_api
from service.api_auth import AuthenticatedPrincipal
from service.api_idempotency import IdempotencyReservation
from service.api_tokens import (
    ADMIN_SCOPE,
    API_GENERATE_SCOPE,
    API_READ_SCOPE,
    API_WRITE_SCOPE,
    MCP_READ_SCOPE,
)
from service.repositories import RegisteredRepository
from service.scm import PullRequestEvent, PushEvent
from service.webhook_server import app

API_TOKEN = "r" * 48


def _principal(
    *scopes: str,
    repository_ids: tuple[int, ...] = (7,),
    all_repositories: bool = False,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        client_id="diffuse-service-token-13",
        subject="rest-test",
        scopes=scopes,
        auth_kind="service_token",
        token_id=13,
        all_repositories=all_repositories,
        repository_ids=() if all_repositories else repository_ids,
        expires_at=None,
    )


@pytest.mark.anyio
async def test_rest_api_uses_problem_details_for_authentication_failures(
    monkeypatch,
):
    async def reject(_token):
        return None

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", reject)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        missing = await client.get("/api/v1/repositories")
        invalid = await client.get(
            "/api/v1/repositories",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )

    assert missing.status_code == 401
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["code"] == "authentication_required"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "invalid_token"
    assert "invalid_token" in invalid.headers["www-authenticate"]


@pytest.mark.anyio
async def test_rest_api_enforces_api_and_generation_scopes(monkeypatch):
    active_principal = _principal(MCP_READ_SCOPE)

    async def authenticate(_token):
        return active_principal

    async def query(*_args, **_kwargs):
        raise AssertionError("A scope failure must happen before data access")

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_query", query)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        read_forbidden = await client.get("/api/v1/repositories")
        active_principal = _principal(API_READ_SCOPE)
        generation_forbidden = await client.post(
            "/api/v1/repositories/7/code/ask",
            json={"question": "How is authorization enforced?"},
        )
        write_forbidden = await client.post(
            "/api/v1/repositories/7/pull-requests/42/reviews",
            headers={"Idempotency-Key": "write-scope-test"},
            json={},
        )
        index_forbidden = await client.post(
            "/api/v1/repositories/7/indexes",
            headers={"Idempotency-Key": "index-write-scope-test"},
            json={},
        )
        onboarding_forbidden = await client.post(
            "/api/v1/repositories",
            headers={"Idempotency-Key": "onboarding-admin-scope-test"},
            json={
                "remote": "github",
                "remoteUrl": "https://github.example.com",
                "name": "owner/repo",
                "defaultBranch": "main",
            },
        )

    assert read_forbidden.status_code == 403
    assert read_forbidden.json()["code"] == "insufficient_scope"
    assert generation_forbidden.status_code == 403
    assert generation_forbidden.json()["code"] == "insufficient_scope"
    assert write_forbidden.status_code == 403
    assert write_forbidden.json()["code"] == "insufficient_scope"
    assert index_forbidden.status_code == 403
    assert index_forbidden.json()["code"] == "insufficient_scope"
    assert onboarding_forbidden.status_code == 403
    assert onboarding_forbidden.json()["code"] == "insufficient_scope"


@pytest.mark.anyio
async def test_rest_repository_onboarding_fetches_exact_commit_and_replays(
    monkeypatch,
):
    principal = _principal(ADMIN_SCOPE, all_repositories=True)
    requested_at = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    repository = RegisteredRepository(
        id=23,
        scm_provider="github",
        scm_base_url="https://github.example.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url="https://github.example.com/owner/repo.git",
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    completed = {
        "apiVersion": "v1",
        "success": True,
        "repository": {
            "id": 23,
            "name": "owner/repo",
            "remote": "github",
            "remoteUrl": "https://github.example.com",
            "defaultBranch": "main",
        },
        "beforeSha": "0" * 40,
        "commitSha": "a" * 40,
        "jobId": 91,
        "queueState": "queued",
    }
    calls = []
    events = []
    mirror_fetches = []
    cached = False

    async def authenticate(_token):
        return principal

    async def mutation(_principal, callback, **kwargs):
        calls.append((callback, kwargs))
        if callback is rest_api._prepare_repository_create:
            return (
                None if cached else repository,
                IdempotencyReservation(
                    id=41,
                    execute=not cached,
                    requested_at=requested_at,
                    operation_data=None,
                    response=completed if cached else None,
                ),
            )
        if callback is rest_api._set_repository_index_syncing:
            return None
        if callback is rest_api._save_repository_index_event:
            events.append(kwargs["event"])
            return None
        if callback is rest_api.enqueue_repository_index_trigger:
            return {
                key: value
                for key, value in completed.items()
                if key != "apiVersion"
            }
        if callback is rest_api._complete_repository_operation:
            return completed
        raise AssertionError(f"Unexpected mutation callback: {callback}")

    class Mirror:
        def __init__(self, target):
            assert target is repository

        def resolve_default_commit(self):
            mirror_fetches.append(True)
            return "a" * 40

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_mutation", mutation)
    monkeypatch.setattr(rest_api, "RepositoryMirror", Mirror)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        first = await client.post(
            "/api/v1/repositories",
            headers={"Idempotency-Key": "onboard-owner-repo"},
            json={
                "remote": "github",
                "remoteUrl": "https://github.example.com",
                "name": "owner/repo",
                "defaultBranch": "main",
            },
        )
        cached = True
        replay = await client.post(
            "/api/v1/repositories",
            headers={"Idempotency-Key": "onboard-owner-repo"},
            json={
                "remote": "github",
                "remoteUrl": "https://github.example.com",
                "name": "owner/repo",
                "defaultBranch": "main",
            },
        )

    assert first.status_code == 202
    assert first.json() == completed
    assert first.headers["location"] == "/api/v1/repositories/23"
    assert replay.status_code == 202
    assert replay.json() == completed
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.headers["location"] == "/api/v1/repositories/23"
    assert mirror_fetches == [True]
    assert len(events) == 1
    assert isinstance(events[0], PushEvent)
    assert events[0].after_sha == "a" * 40
    assert events[0].pushed_at == requested_at.isoformat()
    assert events[0].delivery_id.startswith("api-index-")
    enqueue = next(
        call
        for call in calls
        if call[0] is rest_api.enqueue_repository_index_trigger
    )
    assert enqueue[1]["actor_kind"] == "service_token"
    assert enqueue[1]["actor_token_id"] == 13


@pytest.mark.anyio
async def test_rest_repository_onboarding_requires_all_repository_admin(
    monkeypatch,
):
    async def authenticate(_token):
        return _principal(ADMIN_SCOPE)

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/v1/repositories",
            headers={"Idempotency-Key": "scoped-admin-rejected"},
            json={
                "remote": "github",
                "remoteUrl": "https://github.example.com",
                "name": "owner/repo",
                "defaultBranch": "main",
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "all_repositories_required"


@pytest.mark.anyio
async def test_rest_repository_reindex_recovers_saved_event_without_refetching(
    monkeypatch,
):
    principal = _principal(API_READ_SCOPE, API_WRITE_SCOPE)
    requested_at = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
    repository = RegisteredRepository(
        id=7,
        scm_provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        full_name="group/repo",
        default_branch="main",
        clone_url="https://gitlab.example.com/group/repo.git",
        enabled=True,
        mirror_state="ready",
        last_fetched_sha="a" * 40,
        last_error_code=None,
    )
    event = PushEvent(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        repo_full_name="group/repo",
        ref_name="refs/heads/main",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        pushed_at=requested_at.isoformat(),
        delivery_id="api-index-recovery",
    )
    completed = {
        "apiVersion": "v1",
        "success": True,
        "repository": {"id": 7},
        "commitSha": "b" * 40,
        "jobId": 99,
        "queueState": "queued",
    }
    callbacks = []

    async def authenticate(_token):
        return principal

    async def mutation(_principal, callback, **kwargs):
        callbacks.append(callback)
        if callback is rest_api._prepare_repository_index:
            return (
                repository,
                IdempotencyReservation(
                    id=51,
                    execute=True,
                    requested_at=requested_at,
                    operation_data=event.to_payload(),
                    response=None,
                ),
            )
        if callback is rest_api.enqueue_repository_index_trigger:
            assert kwargs["event"] == event
            return {
                key: value
                for key, value in completed.items()
                if key != "apiVersion"
            }
        if callback is rest_api._complete_repository_operation:
            return completed
        raise AssertionError(f"Unexpected mutation callback: {callback}")

    class Mirror:
        def __init__(self, _repository):
            raise AssertionError("A saved exact event must not be fetched again")

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_mutation", mutation)
    monkeypatch.setattr(rest_api, "RepositoryMirror", Mirror)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/v1/repositories/7/indexes",
            headers={"Idempotency-Key": "recover-index-event"},
            json={},
        )

    assert response.status_code == 202
    assert response.json() == completed
    assert rest_api._set_repository_index_syncing not in callbacks
    assert rest_api._save_repository_index_event not in callbacks


@pytest.mark.anyio
async def test_rest_repository_reindex_records_fetch_failure_and_releases_lease(
    monkeypatch,
):
    principal = _principal(API_READ_SCOPE, API_WRITE_SCOPE)
    requested_at = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
    repository = RegisteredRepository(
        id=7,
        scm_provider="github",
        scm_base_url="https://github.example.com",
        full_name="owner/repo",
        default_branch="main",
        clone_url="https://github.example.com/owner/repo.git",
        enabled=True,
        mirror_state="unconfigured",
        last_fetched_sha=None,
        last_error_code=None,
    )
    callbacks = []

    async def authenticate(_token):
        return principal

    async def mutation(_principal, callback, **_kwargs):
        callbacks.append(callback)
        if callback is rest_api._prepare_repository_index:
            return (
                repository,
                IdempotencyReservation(
                    id=61,
                    execute=True,
                    requested_at=requested_at,
                    operation_data=None,
                    response=None,
                ),
            )
        if callback in {
            rest_api._set_repository_index_syncing,
            rest_api._fail_repository_index_operation,
            rest_api._release_repository_operation,
        }:
            return None
        raise AssertionError(f"Unexpected mutation callback: {callback}")

    class Mirror:
        def __init__(self, _repository):
            pass

        def resolve_default_commit(self):
            raise rest_api.RepositoryMirrorError("provider unavailable")

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_mutation", mutation)
    monkeypatch.setattr(rest_api, "RepositoryMirror", Mirror)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/v1/repositories/7/indexes",
            headers={"Idempotency-Key": "failed-index-fetch"},
            json={},
        )

    assert response.status_code == 502
    assert response.json()["code"] == "repository_unavailable"
    assert rest_api._set_repository_index_syncing in callbacks
    assert rest_api._fail_repository_index_operation in callbacks
    assert rest_api.enqueue_repository_index_trigger not in callbacks


@pytest.mark.anyio
async def test_rest_review_trigger_revalidates_enqueues_and_replays(monkeypatch):
    principal = _principal(API_READ_SCOPE, API_WRITE_SCOPE)
    requested_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    target = {
        "repositoryId": 7,
        "name": "owner/repo",
        "remote": "github",
        "remoteUrl": "https://github.example.com",
        "defaultBranch": "main",
        "pullRequestNumber": 42,
        "headBranch": "feature/auth",
    }
    event = PullRequestEvent(
        provider="github",
        scm_base_url="https://github.example.com",
        api_base_url="https://github.example.com/api/v3",
        repo_full_name="owner/repo",
        number=42,
        web_url="https://github.example.com/owner/repo/pull/42",
        action="manual",
        head_sha="a" * 40,
        base_sha="b" * 40,
        updated_at=requested_at.isoformat(),
        delivery_id="api-test-trigger",
        author="developer",
        base_branch="main",
        head_branch="feature/auth",
        title="Protect authorization",
        trigger_kind="manual",
        trigger_id="api:test-trigger",
        metadata_complete=True,
        changed_file_count=1,
        state="open",
    )
    completed = {
        "apiVersion": "v1",
        "success": True,
        "jobId": 19,
        "queueState": "queued",
    }
    calls = []
    fetches = []
    cached = False

    async def authenticate(_token):
        return principal

    async def mutation(_principal, callback, **kwargs):
        calls.append((callback, kwargs))
        if callback is rest_api._prepare_review_trigger:
            return (
                target,
                IdempotencyReservation(
                    id=31,
                    execute=not cached,
                    requested_at=requested_at,
                    operation_data=None,
                    response=completed if cached else None,
                ),
            )
        if callback is rest_api._save_review_trigger_event:
            return None
        if callback is rest_api.enqueue_review_trigger:
            return {
                "success": True,
                "jobId": 19,
                "queueState": "queued",
            }
        if callback is rest_api._complete_review_trigger:
            return completed
        raise AssertionError(f"Unexpected mutation callback: {callback}")

    async def fetch(**kwargs):
        fetches.append(kwargs)
        return event

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_mutation", mutation)
    monkeypatch.setattr(rest_api, "fetch_current_manual_review_event", fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        first = await client.post(
            "/api/v1/repositories/7/pull-requests/42/reviews",
            headers={"Idempotency-Key": "client-retry-42"},
            json={"branch": "feature/auth"},
        )
        cached = True
        replay = await client.post(
            "/api/v1/repositories/7/pull-requests/42/reviews",
            headers={"Idempotency-Key": "client-retry-42"},
            json={"branch": "feature/auth"},
        )

    assert first.status_code == 202
    assert first.json() == completed
    assert replay.status_code == 202
    assert replay.json() == completed
    assert replay.headers["idempotency-replayed"] == "true"
    assert len(fetches) == 1
    assert fetches[0]["requested_by"] == "rest-test"
    assert fetches[0]["requested_at"] == requested_at.isoformat()
    enqueue = next(call for call in calls if call[0] is rest_api.enqueue_review_trigger)
    assert enqueue[1]["actor_kind"] == "service_token"
    assert enqueue[1]["actor_token_id"] == 13
    assert enqueue[1]["event"] is event


@pytest.mark.anyio
async def test_rest_review_trigger_requires_a_safe_idempotency_key(monkeypatch):
    async def authenticate(_token):
        return _principal(API_READ_SCOPE, API_WRITE_SCOPE)

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        missing = await client.post(
            "/api/v1/repositories/7/pull-requests/42/reviews",
            json={},
        )
        unsafe = await client.post(
            "/api/v1/repositories/7/pull-requests/42/reviews",
            headers={"Idempotency-Key": "contains spaces"},
            json={},
        )

    assert missing.status_code == 422
    assert unsafe.status_code == 422
    assert missing.json()["code"] == "validation_failed"
    assert unsafe.json()["code"] == "validation_failed"


@pytest.mark.anyio
async def test_rest_api_passes_repository_claims_and_bounded_filters(
    monkeypatch,
):
    principal = _principal(API_READ_SCOPE, API_GENERATE_SCOPE)
    calls = []

    async def authenticate(_token):
        return principal

    async def query(received_principal, callback, **kwargs):
        calls.append((received_principal, callback, kwargs))
        if callback is rest_api.list_mcp_repositories:
            return {
                "repositories": [{"id": 7, "name": "owner/repo"}],
                "total": 1,
                "limit": kwargs["limit"],
                "offset": kwargs["offset"],
            }
        if callback is rest_api._run_code_question:
            return {
                "schemaVersion": "diffuse-code-answer-v1",
                "status": "grounded",
                "answer": "Repository authorization is applied in SQL.",
            }
        raise AssertionError(f"Unexpected callback: {callback}")

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_database_query", query)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        repositories = await client.get(
            "/api/v1/repositories?enabled=true&limit=5&offset=10"
        )
        answer = await client.post(
            "/api/v1/repositories/7/code/ask",
            json={
                "question": "How is authorization enforced?",
                "path": "service",
                "includeRelated": True,
                "limit": 6,
            },
        )

    assert repositories.status_code == 200
    assert repositories.json() == {
        "apiVersion": "v1",
        "repositories": [{"id": 7, "name": "owner/repo"}],
        "total": 1,
        "limit": 5,
        "offset": 10,
    }
    assert answer.status_code == 200
    assert answer.json()["apiVersion"] == "v1"
    assert answer.json()["schemaVersion"] == "diffuse-code-answer-v1"
    assert all(call[0] is principal for call in calls)
    assert calls[0][2] == {"enabled": True, "limit": 5, "offset": 10}
    assert calls[1][2]["repository_id"] == 7
    assert calls[1][2]["request"].include_related


@pytest.mark.anyio
async def test_rest_api_hides_unauthorized_resources_and_bounds_json(monkeypatch):
    async def authenticate(_token):
        return _principal(API_READ_SCOPE)

    def fail_not_found(*_args, **_kwargs):
        raise rest_api.ProjectionNotFoundError(
            "Repository does not exist or is not authorized"
        )

    def fail_invalid(*_args, **_kwargs):
        raise ValueError("endAt must be later than startAt")

    monkeypatch.setattr(rest_api, "authenticate_bearer_token", authenticate)
    monkeypatch.setattr(rest_api, "_run_database_query", fail_not_found)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        hidden = await client.get("/api/v1/repositories/99")
        malformed = await client.post(
            "/api/v1/repositories/7/code/search",
            json={
                "query": "authorization",
                "unexpected": "not accepted",
            },
        )
        monkeypatch.setattr(
            rest_api,
            "_run_database_query",
            fail_invalid,
        )
        invalid_window = await client.get(
            "/api/v1/analytics/reviews"
            "?startAt=2026-08-01T00:00:00Z"
            "&endAt=2026-07-01T00:00:00Z"
            "&repository_id=7"
        )

    assert hidden.status_code == 404
    assert hidden.json()["detail"] == (
        "The resource does not exist or is not authorized."
    )
    assert "Repository" not in hidden.text
    assert malformed.status_code == 422
    assert malformed.headers["content-type"].startswith(
        "application/problem+json"
    )
    assert malformed.json()["code"] == "validation_failed"
    assert malformed.json()["errors"][0]["location"] == ["unexpected"]
    assert invalid_window.status_code == 400
    assert invalid_window.json()["code"] == "invalid_request"


@pytest.mark.anyio
async def test_rest_openapi_documents_versioned_bearer_surface():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    security = schema["components"]["securitySchemes"]["DiffuseServiceToken"]
    assert security["type"] == "http"
    assert security["scheme"] == "bearer"
    assert {
        "/api/v1/repositories",
        "/api/v1/repositories/{repository_id}",
        "/api/v1/repositories/{repository_id}/indexes",
        (
            "/api/v1/repositories/{repository_id}/pull-requests/"
            "{pull_request_number}/findings"
        ),
        "/api/v1/reviews/{review_id}",
        "/api/v1/analytics/reviews",
        "/api/v1/repositories/{repository_id}/code/search",
        "/api/v1/repositories/{repository_id}/code/ask",
    }.issubset(schema["paths"])
    review_path = schema["paths"][
        "/api/v1/repositories/{repository_id}/pull-requests/"
        "{pull_request_number}/reviews"
    ]
    assert {"get", "post"}.issubset(review_path)
    idempotency_parameter = next(
        item
        for item in review_path["post"]["parameters"]
        if item["name"] == "Idempotency-Key"
    )
    assert idempotency_parameter["required"]
    repositories_path = schema["paths"]["/api/v1/repositories"]
    assert {"get", "post"}.issubset(repositories_path)
    indexes_path = schema["paths"][
        "/api/v1/repositories/{repository_id}/indexes"
    ]
    assert "post" in indexes_path
    for operation in (repositories_path["post"], indexes_path["post"]):
        parameter = next(
            item
            for item in operation["parameters"]
            if item["name"] == "Idempotency-Key"
        )
        assert parameter["required"]
