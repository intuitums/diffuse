"""Durable, secret-free idempotency records for REST mutations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

import psycopg2.extras

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
RESOURCE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
IDEMPOTENCY_OPERATION_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
IDEMPOTENCY_LEASE_SECONDS = 300
MAX_IDEMPOTENCY_LEASE_SECONDS = 3600


class IdempotencyConflictError(ValueError):
    """A key was reused for a different request."""


class IdempotencyInProgressError(RuntimeError):
    """Another request currently owns the key lease."""


@dataclass(frozen=True)
class IdempotencyReservation:
    id: int
    execute: bool
    requested_at: datetime
    operation_data: dict[str, object] | None
    response: dict[str, object] | None


def validate_idempotency_key(value: str) -> str:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ValueError(
            "Idempotency-Key must contain 1 to 200 URL-safe visible characters"
        )
    return value


def idempotency_key_sha256(value: str) -> str:
    return hashlib.sha256(validate_idempotency_key(value).encode()).hexdigest()


def _resource_hash(value: str, *, field: str) -> str:
    if not RESOURCE_HASH_PATTERN.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _operation(value: str) -> str:
    if not IDEMPOTENCY_OPERATION_PATTERN.fullmatch(value):
        raise ValueError("Idempotency operation is invalid")
    return value


def _actor_identity(value: str) -> str:
    if (
        not 1 <= len(value) <= 255
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Idempotency actor identity is invalid")
    return value


def reserve_idempotency_key(
    conn,
    *,
    actor_identity: str,
    operation: str,
    key_sha256: str,
    request_sha256: str,
    lease_seconds: int = IDEMPOTENCY_LEASE_SECONDS,
) -> IdempotencyReservation:
    actor_identity = _actor_identity(actor_identity)
    operation = _operation(operation)
    key_sha256 = _resource_hash(key_sha256, field="Idempotency-key hash")
    request_sha256 = _resource_hash(request_sha256, field="Request hash")
    if (
        type(lease_seconds) is not int
        or not 1 <= lease_seconds <= MAX_IDEMPOTENCY_LEASE_SECONDS
    ):
        raise ValueError(
            "Idempotency lease must be between 1 and "
            f"{MAX_IDEMPOTENCY_LEASE_SECONDS} seconds"
        )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO api_idempotency_keys (
                actor_identity,
                operation,
                key_sha256,
                request_sha256,
                lease_expires_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                now() + (%s * interval '1 second')
            )
            ON CONFLICT (actor_identity, operation, key_sha256) DO NOTHING
            RETURNING *
            """,
            (
                actor_identity,
                operation,
                key_sha256,
                request_sha256,
                lease_seconds,
            ),
        )
        inserted = cursor.fetchone()
        if inserted is not None:
            return IdempotencyReservation(
                id=int(inserted["id"]),
                execute=True,
                requested_at=inserted["requested_at"],
                operation_data=None,
                response=None,
            )
        cursor.execute(
            """
            SELECT *, lease_expires_at <= now() AS lease_expired
            FROM api_idempotency_keys
            WHERE actor_identity = %s
              AND operation = %s
              AND key_sha256 = %s
            FOR UPDATE
            """,
            (actor_identity, operation, key_sha256),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Idempotency reservation disappeared")
        if row["request_sha256"] != request_sha256:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used for a different request"
            )
        if row["state"] == "completed":
            response = row["response"]
            if not isinstance(response, dict):
                raise RuntimeError("Completed idempotency response is invalid")
            return IdempotencyReservation(
                id=int(row["id"]),
                execute=False,
                requested_at=row["requested_at"],
                operation_data=(
                    dict(row["operation_data"])
                    if isinstance(row["operation_data"], dict)
                    else None
                ),
                response=dict(response),
            )
        if not bool(row["lease_expired"]):
            raise IdempotencyInProgressError(
                "An identical API request is already in progress"
            )
        cursor.execute(
            """
            UPDATE api_idempotency_keys
            SET lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE id = %s
            """,
            (lease_seconds, int(row["id"])),
        )
        return IdempotencyReservation(
            id=int(row["id"]),
            execute=True,
            requested_at=row["requested_at"],
            operation_data=(
                dict(row["operation_data"])
                if isinstance(row["operation_data"], dict)
                else None
            ),
            response=None,
        )


def save_idempotency_operation_data(
    conn,
    *,
    reservation_id: int,
    actor_identity: str,
    operation_data: dict[str, object],
) -> None:
    if reservation_id <= 0:
        raise ValueError("Idempotency reservation ID must be positive")
    actor_identity = _actor_identity(actor_identity)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE api_idempotency_keys
            SET operation_data = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE id = %s
              AND actor_identity = %s
              AND state = 'processing'
            """,
            (
                psycopg2.extras.Json(operation_data),
                IDEMPOTENCY_LEASE_SECONDS,
                reservation_id,
                actor_identity,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Idempotency reservation is no longer active")


def complete_idempotency_key(
    conn,
    *,
    reservation_id: int,
    actor_identity: str,
    response: dict[str, object],
) -> dict[str, object]:
    if reservation_id <= 0:
        raise ValueError("Idempotency reservation ID must be positive")
    actor_identity = _actor_identity(actor_identity)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE api_idempotency_keys
            SET state = 'completed',
                response = %s,
                updated_at = now()
            WHERE id = %s
              AND actor_identity = %s
              AND state = 'processing'
            RETURNING response
            """,
            (
                psycopg2.extras.Json(response),
                reservation_id,
                actor_identity,
            ),
        )
        row = cursor.fetchone()
        if row is None or not isinstance(row["response"], dict):
            raise RuntimeError("Idempotency reservation could not be completed")
        return dict(row["response"])


def release_idempotency_lease(
    conn,
    *,
    reservation_id: int,
    actor_identity: str,
) -> None:
    if reservation_id <= 0:
        return
    actor_identity = _actor_identity(actor_identity)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE api_idempotency_keys
            SET lease_expires_at = now(),
                updated_at = now()
            WHERE id = %s
              AND actor_identity = %s
              AND state = 'processing'
            """,
            (reservation_id, actor_identity),
        )
