import hashlib
import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from service.api_idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    complete_idempotency_key,
    release_idempotency_lease,
    reserve_idempotency_key,
    save_idempotency_operation_data,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
        reason="POSTGRES_TEST_DATABASE_URL is not configured",
    ),
]


def test_idempotency_reservations_recover_and_replay_exact_response():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    actor_identity = f"service_token:test-{suffix}"
    key_hash = hashlib.sha256(f"key-{suffix}".encode()).hexdigest()
    request_hash = hashlib.sha256(f"request-{suffix}".encode()).hexdigest()
    other_request_hash = hashlib.sha256(f"other-{suffix}".encode()).hexdigest()
    operation_data = {"delivery_id": f"api-{suffix}", "head_sha": "a" * 40}
    response = {"apiVersion": "v1", "jobId": 81, "queueState": "queued"}
    try:
        with closing(psycopg2.connect(database_url)) as connection:
            with connection:
                first = reserve_idempotency_key(
                    connection,
                    actor_identity=actor_identity,
                    operation="review.trigger.v1",
                    key_sha256=key_hash,
                    request_sha256=request_hash,
                )
            assert first.execute
            assert first.operation_data is None

            with (
                pytest.raises(
                    IdempotencyInProgressError,
                    match="already in progress",
                ),
                connection,
            ):
                reserve_idempotency_key(
                    connection,
                    actor_identity=actor_identity,
                    operation="review.trigger.v1",
                    key_sha256=key_hash,
                    request_sha256=request_hash,
                )

            with connection:
                save_idempotency_operation_data(
                    connection,
                    reservation_id=first.id,
                    actor_identity=actor_identity,
                    lease_generation=first.lease_generation,
                    operation_data=operation_data,
                )
                release_idempotency_lease(
                    connection,
                    reservation_id=first.id,
                    actor_identity=actor_identity,
                    lease_generation=first.lease_generation,
                )
            with connection:
                recovered = reserve_idempotency_key(
                    connection,
                    actor_identity=actor_identity,
                    operation="review.trigger.v1",
                    key_sha256=key_hash,
                    request_sha256=request_hash,
                )
            assert recovered.execute
            assert recovered.requested_at == first.requested_at
            assert recovered.operation_data == operation_data
            assert recovered.lease_generation == first.lease_generation + 1

            with connection:
                # A stale holder must not fence out the recovered lease.
                with pytest.raises(RuntimeError, match="no longer active"):
                    save_idempotency_operation_data(
                        connection,
                        reservation_id=first.id,
                        actor_identity=actor_identity,
                        lease_generation=first.lease_generation,
                        operation_data={"stale": True},
                    )
                completed = complete_idempotency_key(
                    connection,
                    reservation_id=recovered.id,
                    actor_identity=actor_identity,
                    lease_generation=recovered.lease_generation,
                    response=response,
                )
            assert completed == response
            with connection:
                replay = reserve_idempotency_key(
                    connection,
                    actor_identity=actor_identity,
                    operation="review.trigger.v1",
                    key_sha256=key_hash,
                    request_sha256=request_hash,
                )
            assert not replay.execute
            assert replay.response == response

            with pytest.raises(IdempotencyConflictError), connection:
                reserve_idempotency_key(
                    connection,
                    actor_identity=actor_identity,
                    operation="review.trigger.v1",
                    key_sha256=key_hash,
                    request_sha256=other_request_hash,
                )
    finally:
        with (
            closing(psycopg2.connect(database_url)) as connection,
            connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM api_idempotency_keys WHERE actor_identity = %s",
                (actor_identity,),
            )
