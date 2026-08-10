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
    CONNECT_EVENT_SIGNING_KEY_AAD,
    CONNECT_INSTANCE_TOKEN_AAD,
    EVENT_SIGNING_KEY_AAD,
    credential_kek,
    credential_keks,
    database_url,
    token_key,
)
from .sealed_secret import SealedSecretError, is_sealed, key_id_for, seal, unseal

OAUTH_STATE_LIFETIME = timedelta(minutes=10)
ENROLLMENT_CODE_LIFETIME = timedelta(minutes=15)
CONNECT_SESSION_LIFETIME = timedelta(minutes=15)
EVENT_LEASE_SECONDS = 60
EVENT_RETENTION_DAYS = 30


@contextmanager
def connection() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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


def _event_signing_key_needs_reseal(stored: str) -> bool:
    """True when the row is plaintext or sealed under a previous KEK."""
    if not is_sealed(stored):
        return True
    parts = stored.split(".")
    if len(parts) != 4:
        return True
    return parts[2] != key_id_for(credential_kek())


def _maybe_reseal_event_signing_key(
    cursor: psycopg2.extensions.cursor,
    *,
    instance_id: str,
    stored: str,
    plaintext: str,
) -> None:
    """Best-effort upgrade of legacy / previous-KEK ciphertext onto the current KEK."""
    if not _event_signing_key_needs_reseal(stored):
        return
    try:
        resealed = _seal_event_signing_key(plaintext)
    except Exception:  # noqa: BLE001 — never fail authentication on re-seal
        return
    cursor.execute(
        """
        UPDATE self_hosted_instances
        SET event_signing_key = %s, updated_at = now()
        WHERE id = %s::uuid
          AND revoked_at IS NULL
          AND event_signing_key = %s
        """,
        (resealed, instance_id, stored),
    )


@dataclass(frozen=True)
class OAuthStateTarget:
    installation_id: int | None
    connect_session_id: str | None


def create_oauth_state(
    *,
    installation_id: int | None = None,
    connect_session_id: str | None = None,
) -> str:
    if installation_id is None and connect_session_id is None:
        raise ValueError("OAuth state requires an installation_id or connect_session_id")
    if installation_id is not None and installation_id <= 0:
        raise ValueError("installation_id must be positive")
    state = _random_token()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO setup_oauth_states (
                state_hash, installation_id, connect_session_id, expires_at
            ) VALUES (%s, %s, %s::uuid, now() + interval '10 minutes')
            """,
            (_hash(state), installation_id, connect_session_id),
        )
    return state


def consume_oauth_state(state: str) -> OAuthStateTarget | None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM setup_oauth_states
            WHERE state_hash = %s AND expires_at > now()
            RETURNING installation_id, connect_session_id::text
            """,
            (_hash(state),),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    installation_id = int(row[0]) if row[0] is not None else None
    connect_session_id = str(row[1]) if row[1] is not None else None
    return OAuthStateTarget(
        installation_id=installation_id,
        connect_session_id=connect_session_id,
    )


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


def _insert_instance_credentials(
    cursor: psycopg2.extensions.cursor,
    *,
    installation_id: int,
    display_name: str,
) -> InstanceCredentials:
    instance_token = _random_token()
    event_signing_key = _random_token()
    instance_id = str(uuid.uuid4())
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


def redeem_enrollment_code(code: str, *, display_name: str) -> InstanceCredentials | None:
    if not 1 <= len(display_name.strip()) <= 200:
        raise ValueError("display_name must contain 1 to 200 characters")
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
        return _insert_instance_credentials(
            cursor,
            installation_id=int(row[0]),
            display_name=display_name,
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
        instance_id = str(row[0])
        installation_id = int(row[1])
        stored = str(row[2])
        plaintext = _unseal_event_signing_key(stored)
        _maybe_reseal_event_signing_key(
            cursor,
            instance_id=instance_id,
            stored=stored,
            plaintext=plaintext,
        )
    return Instance(
        id=instance_id,
        installation_id=installation_id,
        event_signing_key=plaintext,
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


def set_installation_active(
    installation_id: int,
    *,
    active: bool,
    revoke_instances: bool = False,
) -> bool:
    """Activate or deactivate a verified installation.

    Uninstall (``revoke_instances=True``) also marks every live self-hosted
    binding revoked so its credential stops pulling events and minting tokens.
    Suspend only flips ``active`` so a later unsuspend can resume delivery.
    """
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE app_installations
            SET active = %s, updated_at = now()
            WHERE github_installation_id = %s
            RETURNING github_installation_id
            """,
            (active, installation_id),
        )
        if cursor.fetchone() is None:
            return False
        if revoke_instances:
            cursor.execute(
                """
                UPDATE self_hosted_instances
                SET revoked_at = now(), updated_at = now()
                WHERE github_installation_id = %s AND revoked_at IS NULL
                """,
                (installation_id,),
            )
        return True


def revoke_instance(token: str) -> bool:
    """Revoke the calling instance's live binding without deleting history."""
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE self_hosted_instances
            SET revoked_at = now(), updated_at = now()
            WHERE credential_hash = %s AND revoked_at IS NULL
            RETURNING id
            """,
            (_hash(token),),
        )
        return cursor.fetchone() is not None


@dataclass(frozen=True)
class InstanceStatus:
    instance_id: str
    installation_id: int
    display_name: str
    installation_active: bool
    pending_events: int
    created_at: str
    updated_at: str


def instance_status(token: str) -> InstanceStatus | None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                instance.id::text,
                instance.github_installation_id,
                instance.display_name,
                installation.active,
                instance.created_at,
                instance.updated_at,
                (
                    SELECT COUNT(*)
                    FROM github_webhook_events AS event
                    LEFT JOIN webhook_event_deliveries AS receipt
                      ON receipt.delivery_id = event.delivery_id
                     AND receipt.instance_id = instance.id
                    WHERE event.github_installation_id = instance.github_installation_id
                      AND (receipt.acknowledged_at IS NULL OR receipt.delivery_id IS NULL)
                ) AS pending_events
            FROM self_hosted_instances AS instance
            JOIN app_installations AS installation
              ON installation.github_installation_id = instance.github_installation_id
            WHERE instance.credential_hash = %s AND instance.revoked_at IS NULL
            """,
            (_hash(token),),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return InstanceStatus(
        instance_id=str(row[0]),
        installation_id=int(row[1]),
        display_name=str(row[2]),
        installation_active=bool(row[3]),
        pending_events=int(row[6]),
        created_at=row[4].isoformat(),
        updated_at=row[5].isoformat(),
    )


@dataclass(frozen=True)
class ConnectSessionCreated:
    session_id: str
    poll_secret: str
    expires_in_seconds: int


@dataclass(frozen=True)
class ConnectSessionRow:
    session_id: str
    display_name: str
    status: str
    installation_id: int | None
    candidate_installations: tuple[dict[str, object], ...]
    authorized_github_user_id: int | None
    authorized_github_login: str | None
    error_message: str | None
    expires_at: str


@dataclass(frozen=True)
class ConnectSessionClaim:
    status: str
    credentials: InstanceCredentials | None = None
    error_message: str | None = None
    expires_in_seconds: int | None = None


def create_connect_session(*, display_name: str) -> ConnectSessionCreated:
    if not 1 <= len(display_name.strip()) <= 200:
        raise ValueError("display_name must contain 1 to 200 characters")
    session_id = str(uuid.uuid4())
    poll_secret = _random_token()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO connect_sessions (
                id, poll_secret_hash, display_name, status, expires_at
            ) VALUES (
                %s::uuid, %s, %s, 'pending', now() + interval '15 minutes'
            )
            """,
            (session_id, _hash(poll_secret), display_name.strip()),
        )
    return ConnectSessionCreated(
        session_id=session_id,
        poll_secret=poll_secret,
        expires_in_seconds=int(CONNECT_SESSION_LIFETIME.total_seconds()),
    )


def get_pending_connect_session(session_id: str) -> ConnectSessionRow | None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id::text,
                display_name,
                status,
                github_installation_id,
                candidate_installations,
                authorized_github_user_id,
                authorized_github_login,
                error_message,
                expires_at
            FROM connect_sessions
            WHERE id = %s::uuid
              AND expires_at > now()
              AND status IN ('pending', 'ready')
            """,
            (session_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    candidates_raw = row[4] or []
    if isinstance(candidates_raw, str):
        candidates_raw = json.loads(candidates_raw)
    candidates = tuple(dict(item) for item in candidates_raw)
    return ConnectSessionRow(
        session_id=str(row[0]),
        display_name=str(row[1]),
        status=str(row[2]),
        installation_id=int(row[3]) if row[3] is not None else None,
        candidate_installations=candidates,
        authorized_github_user_id=int(row[5]) if row[5] is not None else None,
        authorized_github_login=str(row[6]) if row[6] is not None else None,
        error_message=str(row[7]) if row[7] is not None else None,
        expires_at=row[8].isoformat(),
    )


def set_connect_session_candidates(
    session_id: str,
    *,
    candidates: tuple[dict[str, object], ...],
    github_user_id: int,
    github_login: str,
) -> bool:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE connect_sessions
            SET candidate_installations = %s::jsonb,
                authorized_github_user_id = %s,
                authorized_github_login = %s,
                updated_at = now()
            WHERE id = %s::uuid
              AND status = 'pending'
              AND expires_at > now()
            RETURNING id
            """,
            (json.dumps(list(candidates)), github_user_id, github_login, session_id),
        )
        return cursor.fetchone() is not None


def fail_connect_session(session_id: str, message: str) -> None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE connect_sessions
            SET status = 'failed',
                error_message = %s,
                candidate_installations = NULL,
                updated_at = now()
            WHERE id = %s::uuid AND status = 'pending'
            """,
            (message[:500], session_id),
        )


def complete_connect_session(
    session_id: str,
    *,
    installation_id: int,
    github_user_id: int,
    github_login: str,
    allowed_installation_ids: set[int] | None = None,
) -> InstanceCredentials | None:
    if installation_id <= 0:
        raise ValueError("installation_id must be positive")
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT display_name, status, candidate_installations
            FROM connect_sessions
            WHERE id = %s::uuid
              AND expires_at > now()
            FOR UPDATE
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None or str(row[1]) != "pending":
            return None
        display_name = str(row[0])
        allowed = allowed_installation_ids
        if allowed is None and row[2] is not None:
            candidates = row[2]
            if isinstance(candidates, str):
                candidates = json.loads(candidates)
            allowed = {int(item["id"]) for item in candidates}
        if allowed is not None and installation_id not in allowed:
            return None
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
        credentials = _insert_instance_credentials(
            cursor,
            installation_id=installation_id,
            display_name=display_name,
        )
        cursor.execute(
            """
            UPDATE connect_sessions
            SET status = 'ready',
                github_installation_id = %s,
                instance_id = %s::uuid,
                instance_token_sealed = %s,
                event_signing_key_sealed = %s,
                candidate_installations = NULL,
                updated_at = now()
            WHERE id = %s::uuid AND status = 'pending'
            RETURNING id
            """,
            (
                installation_id,
                credentials.instance_id,
                seal(
                    credentials.instance_token,
                    kek=credential_kek(),
                    aad=CONNECT_INSTANCE_TOKEN_AAD,
                ),
                seal(
                    credentials.event_signing_key,
                    kek=credential_kek(),
                    aad=CONNECT_EVENT_SIGNING_KEY_AAD,
                ),
                session_id,
            ),
        )
        if cursor.fetchone() is None:
            return None
    return credentials


def claim_connect_session(session_id: str, poll_secret: str) -> ConnectSessionClaim:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                status,
                instance_id::text,
                github_installation_id,
                instance_token_sealed,
                event_signing_key_sealed,
                error_message,
                EXTRACT(EPOCH FROM (expires_at - now()))
            FROM connect_sessions
            WHERE id = %s::uuid AND poll_secret_hash = %s
            FOR UPDATE
            """,
            (session_id, _hash(poll_secret)),
        )
        row = cursor.fetchone()
        if row is None:
            return ConnectSessionClaim(status="not_found")
        status = str(row[0])
        expires_in = max(0, int(row[6] or 0))
        if status == "pending":
            if expires_in <= 0:
                cursor.execute(
                    """
                    UPDATE connect_sessions
                    SET status = 'failed',
                        error_message = 'Connect session expired',
                        updated_at = now()
                    WHERE id = %s::uuid AND status = 'pending'
                    """,
                    (session_id,),
                )
                return ConnectSessionClaim(
                    status="expired",
                    error_message="Connect session expired",
                )
            return ConnectSessionClaim(status="pending", expires_in_seconds=expires_in)
        if status == "ready":
            instance_id = str(row[1])
            installation_id = int(row[2])
            try:
                instance_token = unseal(
                    str(row[3]),
                    keks=credential_keks(),
                    aad=CONNECT_INSTANCE_TOKEN_AAD,
                )
                event_signing_key = unseal(
                    str(row[4]),
                    keks=credential_keks(),
                    aad=CONNECT_EVENT_SIGNING_KEY_AAD,
                )
            except SealedSecretError as error:
                raise ValueError("stored connect credentials could not be decrypted") from error
            cursor.execute(
                """
                UPDATE connect_sessions
                SET status = 'consumed',
                    instance_token_sealed = NULL,
                    event_signing_key_sealed = NULL,
                    updated_at = now()
                WHERE id = %s::uuid AND status = 'ready'
                RETURNING id
                """,
                (session_id,),
            )
            if cursor.fetchone() is None:
                return ConnectSessionClaim(status="consumed")
            return ConnectSessionClaim(
                status="ready",
                credentials=InstanceCredentials(
                    instance_id=instance_id,
                    instance_token=instance_token,
                    event_signing_key=event_signing_key,
                    installation_id=installation_id,
                ),
            )
        if status == "consumed":
            return ConnectSessionClaim(status="consumed")
        return ConnectSessionClaim(
            status="failed" if status == "failed" else status,
            error_message=str(row[5]) if row[5] is not None else None,
        )
