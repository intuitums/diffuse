import os
import uuid

import psycopg2

from service.api_tokens import (
    MCP_GENERATE_SCOPE,
    MCP_READ_SCOPE,
    api_token_sha256,
    create_service_token,
    list_service_tokens,
    load_service_token_access,
    revoke_service_token,
)
from service.repositories import register_repository


def test_service_tokens_are_hashed_scoped_audited_and_revocable():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex
    credential = "t" * 32 + suffix
    try:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.example.com",
            full_name=f"tokens/{suffix}",
            default_branch="main",
        )
        record = create_service_token(
            connection,
            name=f"token-{suffix}",
            token=credential,
            scopes=(MCP_READ_SCOPE, MCP_GENERATE_SCOPE),
            repository_ids=(repository.id,),
            actor="integration-test",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT token_sha256 FROM api_tokens WHERE id = %s",
                (record.id,),
            )
            stored_hash = cursor.fetchone()[0]
        assert stored_hash == api_token_sha256(credential)
        assert stored_hash != credential
        assert record.scopes == (MCP_READ_SCOPE, MCP_GENERATE_SCOPE)
        assert record.repository_ids == (repository.id,)
        assert not hasattr(record, "token")
        assert not hasattr(record, "token_sha256")

        loaded = load_service_token_access(
            connection,
            token_sha256=api_token_sha256(credential),
        )
        assert loaded is not None
        assert loaded.scopes == (MCP_READ_SCOPE, MCP_GENERATE_SCOPE)
        assert loaded.repository_ids == (repository.id,)
        assert (
            load_service_token_access(
                connection,
                token_sha256=api_token_sha256("w" * 64),
            )
            is None
        )
        assert any(item.id == record.id for item in list_service_tokens(connection))

        revoked = revoke_service_token(
            connection,
            token_id=record.id,
            actor="integration-test",
            reason="credential rotation",
        )
        assert revoked.revoked_at is not None
        assert (
            load_service_token_access(
                connection,
                token_sha256=api_token_sha256(credential),
            )
            is None
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT action
                FROM audit_events
                WHERE resource_kind = 'api_token'
                  AND resource_id = %s
                ORDER BY id
                """,
                (str(record.id),),
            )
            assert [row[0] for row in cursor.fetchall()] == [
                "api_token.created",
                "api_token.revoked",
            ]
    finally:
        connection.rollback()
        connection.close()
