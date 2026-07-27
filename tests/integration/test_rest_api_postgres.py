import hashlib
import json
import os
import uuid
from contextlib import closing
from datetime import UTC, datetime

import httpx
import psycopg2
import pytest

from service import rest_api
from service.api_tokens import (
    API_READ_SCOPE,
    API_WRITE_SCOPE,
    MCP_READ_SCOPE,
    create_service_token,
)
from service.scm import PullRequestEvent
from service.webhook_server import app
from service.workflow import enqueue_review_event


@pytest.mark.anyio
async def test_rest_service_tokens_enforce_scope_and_repository_grants(
    monkeypatch,
):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    read_credential = "a" * 32 + suffix
    mcp_credential = "b" * 32 + suffix
    repository_ids: list[int] = []
    token_ids: list[int] = []
    with closing(psycopg2.connect(database_url)) as connection:
        with connection, connection.cursor() as cursor:
            for name in (f"rest/allowed-{suffix}", f"rest/hidden-{suffix}"):
                cursor.execute(
                    """
                    INSERT INTO repositories (
                        scm_provider,
                        scm_base_url,
                        full_name,
                        default_branch
                    )
                    VALUES ('github', 'https://github.example.com', %s, 'main')
                    RETURNING id
                    """,
                    (name,),
                )
                repository_ids.append(int(cursor.fetchone()[0]))
        read_token = create_service_token(
            connection,
            name=f"rest-read-{suffix}",
            token=read_credential,
            scopes=(API_READ_SCOPE,),
            repository_ids=(repository_ids[0],),
            actor="integration-test",
        )
        mcp_token = create_service_token(
            connection,
            name=f"rest-mcp-{suffix}",
            token=mcp_credential,
            scopes=(MCP_READ_SCOPE,),
            repository_ids=(repository_ids[0],),
            actor="integration-test",
        )
        token_ids.extend((read_token.id, mcp_token.id))
        connection.commit()

    monkeypatch.delenv("DIFFUSE_API_TOKEN", raising=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            allowed = await client.get(
                "/api/v1/repositories",
                headers={"Authorization": f"Bearer {read_credential}"},
            )
            hidden = await client.get(
                f"/api/v1/repositories/{repository_ids[1]}",
                headers={"Authorization": f"Bearer {read_credential}"},
            )
            wrong_scope = await client.get(
                "/api/v1/repositories",
                headers={"Authorization": f"Bearer {mcp_credential}"},
            )

        assert allowed.status_code == 200
        assert [item["id"] for item in allowed.json()["repositories"]] == [
            repository_ids[0]
        ]
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "not_found"
        assert wrong_scope.status_code == 403
        assert wrong_scope.json()["code"] == "insufficient_scope"
    finally:
        with (
            closing(psycopg2.connect(database_url)) as connection,
            connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM api_tokens WHERE id = ANY(%s)",
                (token_ids,),
            )
            cursor.execute(
                "DELETE FROM repositories WHERE id = ANY(%s)",
                (repository_ids,),
            )


@pytest.mark.anyio
async def test_rest_review_trigger_is_durable_audited_and_idempotent(
    monkeypatch,
):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    credential = "c" * 32 + suffix
    idempotency_key = f"integration-review-{suffix}"
    repository_name = f"rest/trigger-{suffix}"
    opened_at = datetime.now(UTC)
    repository_id = 0
    token_id = 0
    actor_identity = ""
    fetches = []
    try:
        with closing(psycopg2.connect(database_url)) as connection:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO repositories (
                        scm_provider,
                        scm_base_url,
                        full_name,
                        default_branch
                    )
                    VALUES ('github', 'https://github.example.com', %s, 'main')
                    RETURNING id
                    """,
                    (repository_name,),
                )
                repository_id = int(cursor.fetchone()[0])
            token = create_service_token(
                connection,
                name=f"rest-trigger-{suffix}",
                token=credential,
                scopes=(API_READ_SCOPE, API_WRITE_SCOPE),
                repository_ids=(repository_id,),
                actor="integration-test",
            )
            token_id = token.id
            actor_identity = f"service_token:{token_id}"
            common = {
                "provider": "github",
                "scm_base_url": "https://github.example.com",
                "api_base_url": "https://github.example.com/api/v3",
                "repo_full_name": repository_name,
                "number": 42,
                "web_url": (
                    f"https://github.example.com/{repository_name}/pull/42"
                ),
                "head_sha": "d" * 40,
                "base_sha": "e" * 40,
                "author": "developer",
                "base_branch": "main",
                "head_branch": "feature/rest",
                "title": "Add the REST review trigger",
                "description": "Exercises durable API idempotency.",
                "metadata_complete": True,
                "changed_file_count": 1,
                "state": "open",
                "source_created_at": opened_at.isoformat(),
            }
            opened = PullRequestEvent(
                **common,
                action="opened",
                updated_at=opened_at.isoformat(),
                delivery_id=f"opened-{suffix}",
            )
            serialized = json.dumps(
                opened.to_payload(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            enqueue_review_event(
                connection,
                opened,
                payload_sha256=hashlib.sha256(serialized).hexdigest(),
            )
            connection.commit()

        async def fetch(**kwargs):
            fetches.append(kwargs)
            return PullRequestEvent(
                **common,
                action="manual",
                updated_at=kwargs["requested_at"],
                delivery_id=f"api-{suffix}",
                trigger_kind="manual",
                trigger_id=f"api:{suffix}",
            )

        monkeypatch.delenv("DIFFUSE_API_TOKEN", raising=False)
        monkeypatch.setattr(
            rest_api,
            "fetch_current_manual_review_event",
            fetch,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {credential}"},
        ) as client:
            first = await client.post(
                f"/api/v1/repositories/{repository_id}"
                "/pull-requests/42/reviews",
                headers={"Idempotency-Key": idempotency_key},
                json={},
            )
            replay = await client.post(
                f"/api/v1/repositories/{repository_id}"
                "/pull-requests/42/reviews",
                headers={"Idempotency-Key": idempotency_key},
                json={},
            )
            conflict = await client.post(
                f"/api/v1/repositories/{repository_id}"
                "/pull-requests/42/reviews",
                headers={"Idempotency-Key": idempotency_key},
                json={"branch": "feature/rest"},
            )

        assert first.status_code == 202
        assert first.json()["success"]
        assert first.json()["queueState"] == "queued"
        assert replay.status_code == 202
        assert replay.json() == first.json()
        assert replay.headers["idempotency-replayed"] == "true"
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"
        assert len(fetches) == 1

        with (
            closing(psycopg2.connect(database_url)) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT state, key_sha256, response
                FROM api_idempotency_keys
                WHERE actor_identity = %s
                  AND operation = 'review.trigger.v1'
                """,
                (actor_identity,),
            )
            state, key_hash, stored_response = cursor.fetchone()
            assert state == "completed"
            assert key_hash != idempotency_key
            assert stored_response == first.json()
            cursor.execute(
                """
                SELECT count(*)
                FROM audit_events
                WHERE repository_id = %s
                  AND actor_kind = 'service_token'
                  AND actor_label = %s
                  AND action = 'code_review.triggered'
                """,
                (repository_id, f"rest-trigger-{suffix}"),
            )
            assert cursor.fetchone()[0] == 1
    finally:
        if repository_id and token_id:
            with (
                closing(psycopg2.connect(database_url)) as connection,
                connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "DELETE FROM api_idempotency_keys WHERE actor_identity = %s",
                    (actor_identity,),
                )
                cursor.execute(
                    "DELETE FROM api_tokens WHERE id = %s",
                    (token_id,),
                )
                cursor.execute(
                    "DELETE FROM repositories WHERE id = %s",
                    (repository_id,),
                )


@pytest.mark.anyio
async def test_rest_repository_onboarding_and_reindex_are_durable_and_idempotent(
    monkeypatch,
):
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    credential = "o" * 32 + suffix
    base_url = "https://github.example.com"
    repository_name = f"rest/onboard-{suffix}"
    create_key = f"create-{suffix}"
    index_key = f"index-{suffix}"
    create_key_hash = hashlib.sha256(create_key.encode()).hexdigest()
    index_key_hash = hashlib.sha256(index_key.encode()).hexdigest()
    actor_identity = "operator:self-hosted-operator"
    create_delivery = "api-index-" + hashlib.sha256(
        (
            f"{rest_api.REPOSITORY_CREATE_OPERATION}\0"
            f"{actor_identity}\0{create_key_hash}"
        ).encode()
    ).hexdigest()[:48]
    index_delivery = "api-index-" + hashlib.sha256(
        (
            f"{rest_api.REPOSITORY_INDEX_OPERATION}\0"
            f"{actor_identity}\0{index_key_hash}"
        ).encode()
    ).hexdigest()[:48]
    repository_id = 0
    mirror_fetches = []

    class Mirror:
        def __init__(self, repository):
            mirror_fetches.append(repository.id)

        def resolve_default_commit(self):
            return "f" * 40

    monkeypatch.setenv("DIFFUSE_API_TOKEN", credential)
    monkeypatch.setenv("GITHUB_WEB_URL", base_url)
    monkeypatch.setenv("GITHUB_API_URL", f"{base_url}/api/v3")
    monkeypatch.setattr(rest_api, "RepositoryMirror", Mirror)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {credential}"},
        ) as client:
            first = await client.post(
                "/api/v1/repositories",
                headers={"Idempotency-Key": create_key},
                json={
                    "remote": "github",
                    "remoteUrl": base_url,
                    "name": repository_name,
                    "defaultBranch": "main",
                },
            )
            replay = await client.post(
                "/api/v1/repositories",
                headers={"Idempotency-Key": create_key},
                json={
                    "remote": "github",
                    "remoteUrl": base_url,
                    "name": repository_name,
                    "defaultBranch": "main",
                },
            )
            conflict = await client.post(
                "/api/v1/repositories",
                headers={"Idempotency-Key": create_key},
                json={
                    "remote": "github",
                    "remoteUrl": base_url,
                    "name": repository_name,
                    "defaultBranch": "develop",
                },
            )

            assert first.status_code == 202
            repository_id = first.json()["repository"]["id"]
            assert first.json()["queueState"] == "queued"
            assert first.json()["commitSha"] == "f" * 40
            assert first.headers["location"] == (
                f"/api/v1/repositories/{repository_id}"
            )
            assert replay.status_code == 202
            assert replay.json() == first.json()
            assert replay.headers["idempotency-replayed"] == "true"
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "idempotency_conflict"

            reindex = await client.post(
                f"/api/v1/repositories/{repository_id}/indexes",
                headers={"Idempotency-Key": index_key},
                json={},
            )
            reindex_replay = await client.post(
                f"/api/v1/repositories/{repository_id}/indexes",
                headers={"Idempotency-Key": index_key},
                json={},
            )

        assert reindex.status_code == 202
        assert reindex.json()["jobId"] == first.json()["jobId"]
        assert reindex.json()["queueState"].startswith("duplicate_revision:")
        assert reindex_replay.status_code == 202
        assert reindex_replay.json() == reindex.json()
        assert reindex_replay.headers["idempotency-replayed"] == "true"
        assert mirror_fetches == [repository_id, repository_id]

        with (
            closing(psycopg2.connect(database_url)) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT mirror_state, last_fetched_sha
                FROM repositories
                WHERE id = %s
                """,
                (repository_id,),
            )
            assert cursor.fetchone() == ("ready", "f" * 40)
            cursor.execute(
                """
                SELECT count(*)
                FROM workflow_jobs
                WHERE repository_id = %s
                  AND job_type = 'index_repository'
                """,
                (repository_id,),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                """
                SELECT operation, state, response
                FROM api_idempotency_keys
                WHERE actor_identity = %s
                  AND key_sha256 = ANY(%s)
                ORDER BY operation
                """,
                (actor_identity, [create_key_hash, index_key_hash]),
            )
            idempotency_rows = cursor.fetchall()
            assert [row[0] for row in idempotency_rows] == [
                rest_api.REPOSITORY_CREATE_OPERATION,
                rest_api.REPOSITORY_INDEX_OPERATION,
            ]
            assert all(row[1] == "completed" for row in idempotency_rows)
            assert all(row[2]["commitSha"] == "f" * 40 for row in idempotency_rows)
            cursor.execute(
                """
                SELECT action, count(*)
                FROM audit_events
                WHERE repository_id = %s
                  AND action IN (
                      'repository.onboarded',
                      'repository.index_requested'
                  )
                GROUP BY action
                ORDER BY action
                """,
                (repository_id,),
            )
            assert cursor.fetchall() == [
                ("repository.index_requested", 2),
                ("repository.onboarded", 1),
            ]
    finally:
        with (
            closing(psycopg2.connect(database_url)) as connection,
            connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                DELETE FROM api_idempotency_keys
                WHERE actor_identity = %s
                  AND key_sha256 = ANY(%s)
                """,
                (actor_identity, [create_key_hash, index_key_hash]),
            )
            cursor.execute(
                """
                DELETE FROM scm_webhook_deliveries
                WHERE scm_provider = 'github'
                  AND scm_base_url = %s
                  AND delivery_id = ANY(%s)
                """,
                (base_url, [create_delivery, index_delivery]),
            )
            if repository_id:
                cursor.execute(
                    "DELETE FROM repositories WHERE id = %s",
                    (repository_id,),
                )
