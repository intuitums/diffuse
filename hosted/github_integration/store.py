"""PostgreSQL persistence for the deliberately small hosted integration service."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import psycopg2
import psycopg2.extras

from .config import (
    EVENT_SIGNING_KEY_AAD,
    credential_kek,
    credential_keks,
    database_url,
    token_key,
)
from .sealed_secret import SealedSecretError, seal, unseal

OAUTH_STATE_LIFETIME = timedelta(minutes=10)
ENROLLMENT_CODE_LIFETIME = timedelta(minutes=15)
EVENT_LEASE_SECONDS = 60
EVENT_RETENTION_DAYS = 30


@contextmanager
def connection() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(database_url())
    try:
        yield conn
    finally:
        conn.close()


def initialize_schema() -> None:
    """Apply the idempotent hosted-only schema during an explicit setup operation."""
    from pathlib import Path

    schema = Path(__file__).with_name("schema.sql").read_text()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(schema)


def _hash(value: str) -> str:
    return hmac.new(token_key(), value.encode(), hashlib.sha256).hexdigest()


def _random_token() -> str:
    return secrets.token_urlsafe(32)


def _seal_event_signing_key(value: str) -> str:
    return seal(value, kek=credential_kek(), aad=EVENT_SIGNING_KEY_AAD)


def _unseal_event_signing_key(value: str) -> str:
    try:
        return unseal(
            value,
            keks=credential_keks(),
            aad=EVENT_SIGNING_KEY_AAD,
            allow_legacy_plaintext=True,
        )
    except SealedSecretError as error:
        raise ValueError("stored event signing key could not be decrypted") from error


def create_oauth_state(installation_id: int) -> str:
    state = _random_token()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO setup_oauth_states (state_hash, installation_id, expires_at)
            VALUES (%s, %s, now() + interval '10 minutes')
            """,
            (_hash(state), installation_id),
        )
    return state


def consume_oauth_state(state: str) -> int | None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM setup_oauth_states
            WHERE state_hash = %s AND expires_at > now()
            RETURNING installation_id
            """,
            (_hash(state),),
        )
        row = cursor.fetchone()
    return int(row[0]) if row else None


def record_verified_installation(
    installation_id: int,
    *,
    github_user_id: int,
    github_login: str,
) -> None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app_installations (
                github_installation_id, github_user_id, github_login, active
            ) VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (github_installation_id) DO UPDATE SET
                github_user_id = EXCLUDED.github_user_id,
                github_login = EXCLUDED.github_login,
                active = TRUE,
                updated_at = now()
            """,
            (installation_id, github_user_id, github_login),
        )


def create_enrollment_code(installation_id: int) -> str:
    code = _random_token()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO setup_enrollment_codes (
                code_hash, github_installation_id, expires_at
            ) VALUES (%s, %s, now() + interval '15 minutes')
            """,
            (_hash(code), installation_id),
        )
    return code


@dataclass(frozen=True)
class InstanceCredentials:
    instance_id: str
    instance_token: str
    event_signing_key: str
    installation_id: int


def redeem_enrollment_code(code: str, *, display_name: str) -> InstanceCredentials | None:
    if not 1 <= len(display_name.strip()) <= 200:
        raise ValueError("display_name must contain 1 to 200 characters")
    instance_token = _random_token()
    event_signing_key = _random_token()
    instance_id = str(uuid.uuid4())
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE setup_enrollment_codes
            SET redeemed_at = now()
            WHERE code_hash = %s
              AND redeemed_at IS NULL
              AND expires_at > now()
            RETURNING github_installation_id
            """,
            (_hash(code),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        installation_id = int(row[0])
        cursor.execute(
            """
            INSERT INTO self_hosted_instances (
                id, github_installation_id, display_name, credential_hash, event_signing_key
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (github_installation_id) WHERE revoked_at IS NULL
            DO UPDATE SET
                display_name = EXCLUDED.display_name,
                credential_hash = EXCLUDED.credential_hash,
                event_signing_key = EXCLUDED.event_signing_key,
                updated_at = now()
            RETURNING id::text
            """,
            (
                instance_id,
                installation_id,
                display_name.strip(),
                _hash(instance_token),
                _seal_event_signing_key(event_signing_key),
            ),
        )
        stored_instance_id = str(cursor.fetchone()[0])
    return InstanceCredentials(
        instance_id=stored_instance_id,
        instance_token=instance_token,
        event_signing_key=event_signing_key,
        installation_id=installation_id,
    )


@dataclass(frozen=True)
class Instance:
    id: str
    installation_id: int
    event_signing_key: str


def authenticate_instance(token: str) -> Instance | None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id::text, github_installation_id, event_signing_key
            FROM self_hosted_instances
            WHERE credential_hash = %s AND revoked_at IS NULL
            """,
            (_hash(token),),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return Instance(
        id=str(row[0]),
        installation_id=int(row[1]),
        event_signing_key=_unseal_event_signing_key(str(row[2])),
    )


def record_webhook_event(
    *,
    delivery_id: str,
    installation_id: int,
    event_name: str,
    payload: dict,
    payload_sha256: str,
) -> bool:
    """Store an event only for an enrolled active installation, once per delivery."""
    with connection() as conn, conn.cursor() as cursor:
        # A disconnected instance must not turn this thin delivery service into a permanent
        # event archive. Acknowledged events are deleted immediately below; this
        # bounds exceptional, never-acknowledged deliveries during normal ingress.
        cursor.execute(
            "DELETE FROM github_webhook_events WHERE received_at < now() - %s * interval '1 day'",
            (EVENT_RETENTION_DAYS,),
        )
        cursor.execute(
            """
            INSERT INTO github_webhook_events (
                delivery_id, github_installation_id, event_name, payload, payload_sha256
            )
            SELECT %s, installation.github_installation_id, %s, %s::jsonb, %s
            FROM app_installations AS installation
            WHERE installation.github_installation_id = %s AND installation.active
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING delivery_id
            """,
            (
                delivery_id,
                event_name,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                payload_sha256,
                installation_id,
            ),
        )
        return cursor.fetchone() is not None


@dataclass(frozen=True)
class DeliveryEvent:
    delivery_id: str
    event_name: str
    payload: dict
    signature: str


def pull_events(instance: Instance, *, limit: int = 20) -> tuple[DeliveryEvent, ...]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    with connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            WITH available AS (
                SELECT event.delivery_id, event.event_name, event.payload
                FROM github_webhook_events AS event
                LEFT JOIN webhook_event_deliveries AS receipt
                  ON receipt.delivery_id = event.delivery_id
                 AND receipt.instance_id = %s::uuid
                WHERE event.github_installation_id = %s
                  AND (receipt.acknowledged_at IS NULL OR receipt.delivery_id IS NULL)
                  AND (receipt.leased_until IS NULL OR receipt.leased_until < now())
                ORDER BY event.received_at
                LIMIT %s
                FOR UPDATE OF event SKIP LOCKED
            ), claimed AS (
                INSERT INTO webhook_event_deliveries (
                    delivery_id, instance_id, attempts, leased_until
                )
                SELECT delivery_id, %s::uuid, 1, now() + interval '60 seconds'
                FROM available
                ON CONFLICT (delivery_id, instance_id) DO UPDATE SET
                    attempts = webhook_event_deliveries.attempts + 1,
                    leased_until = now() + interval '60 seconds'
                RETURNING delivery_id
            )
            SELECT available.delivery_id, available.event_name, available.payload
            FROM available JOIN claimed USING (delivery_id)
            """,
            (instance.id, instance.installation_id, limit, instance.id),
        )
        rows = cursor.fetchall()
    events = []
    for row in rows:
        payload = dict(row["payload"])
        canonical = json.dumps(
            {"delivery": row["delivery_id"], "event": row["event_name"], "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        events.append(
            DeliveryEvent(
                delivery_id=str(row["delivery_id"]),
                event_name=str(row["event_name"]),
                payload=payload,
                signature=hmac.new(
                    instance.event_signing_key.encode(), canonical, hashlib.sha256
                ).hexdigest(),
            )
        )
    return tuple(events)


def acknowledge_event(instance: Instance, delivery_id: str) -> bool:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH acknowledged AS (
                UPDATE webhook_event_deliveries
                SET acknowledged_at = now(), leased_until = NULL
                WHERE instance_id = %s::uuid
                  AND delivery_id = %s
                  AND acknowledged_at IS NULL
                RETURNING delivery_id
            ), removed AS (
                DELETE FROM github_webhook_events AS event
                USING acknowledged
                WHERE event.delivery_id = acknowledged.delivery_id
                RETURNING event.delivery_id
            )
            SELECT EXISTS (SELECT 1 FROM removed)
            """,
            (instance.id, delivery_id),
        )
        return bool(cursor.fetchone()[0])
