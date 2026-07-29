"""Durable routing state for the hosted Diffuse integration relay."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

import psycopg2.extras

MAX_IDENTIFIER = 9_223_372_036_854_775_807
MAX_DELIVERY_BYTES = 1_000_000
MAX_DELIVERY_ATTEMPTS = 100
DEFAULT_PAIRING_TTL_SECONDS = 600
MAX_PAIRING_TTL_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 600
MAX_NODE_NAME_CHARS = 255

PAIRING_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,128}")
NODE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,128}")
EVENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
DELIVERY_ID_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")


class RelayStoreError(RuntimeError):
    """Relay state could not be changed safely."""


class InvalidPairingCodeError(RelayStoreError):
    """A pairing code is unknown, expired, or already consumed."""


class RelayDeliveryConflictError(RelayStoreError):
    """A provider reused a delivery id for different content or routing."""


class InstallationOwnershipError(RelayStoreError):
    """The signed installation event does not attribute this install to the user."""


@dataclass(frozen=True)
class RelayNode:
    id: int
    user_id: int
    github_installation_id: int
    name: str
    last_seen_at: datetime | None


@dataclass(frozen=True)
class LeasedRelayDelivery:
    id: int
    provider: str
    provider_delivery_id: str
    event_name: str
    payload: bytes
    payload_sha256: str
    attempt_count: int
    leased_until: datetime


def _positive_identifier(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_IDENTIFIER
    ):
        raise ValueError(f"{field_name} must be a positive identifier")
    return value


def _bounded_seconds(value: int, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum} seconds")
    return value


def validate_node_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Relay node name must be text")
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= MAX_NODE_NAME_CHARS
        or "\x00" in normalized
        or any(character in "\r\n" for character in normalized)
    ):
        raise ValueError(
            f"Relay node name must contain 1 to {MAX_NODE_NAME_CHARS} visible characters"
        )
    return normalized


def generate_pairing_code() -> str:
    return secrets.token_urlsafe(32)


def generate_node_token() -> str:
    return secrets.token_urlsafe(32)


def _secret_sha256(value: str, *, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")
    return hashlib.sha256(value.encode()).hexdigest()


def pairing_code_sha256(value: str) -> str:
    return _secret_sha256(
        value,
        pattern=PAIRING_CODE_PATTERN,
        field_name="Relay pairing code",
    )


def node_token_sha256(value: str) -> str:
    return _secret_sha256(
        value,
        pattern=NODE_TOKEN_PATTERN,
        field_name="Relay node token",
    )


def _node_from_row(row) -> RelayNode:
    return RelayNode(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        github_installation_id=int(row["github_installation_id"]),
        name=row["name"],
        last_seen_at=row["last_seen_at"],
    )


def create_pairing_code(
    conn,
    *,
    user_id: int,
    github_installation_id: int,
    ttl_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
) -> str:
    user = _positive_identifier(user_id, field_name="user_id")
    installation = _positive_identifier(
        github_installation_id,
        field_name="github_installation_id",
    )
    ttl = _bounded_seconds(
        ttl_seconds,
        field_name="Pairing-code lifetime",
        maximum=MAX_PAIRING_TTL_SECONDS,
    )
    code = generate_pairing_code()
    digest = pairing_code_sha256(code)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE relay_pairing_codes
            SET consumed_at = now()
            WHERE github_installation_id = %s
              AND consumed_at IS NULL
            """,
            (installation,),
        )
        cursor.execute(
            """
            INSERT INTO relay_pairing_codes (
                user_id,
                github_installation_id,
                code_sha256,
                expires_at
            )
            VALUES (%s, %s, %s, now() + make_interval(secs => %s))
            """,
            (user, installation, digest, ttl),
        )
    return code


def record_github_installation_event(conn, *, payload: dict) -> None:
    """Record install ownership only from GitHub's HMAC-verified installation event."""
    try:
        action = payload["action"]
        installation = payload["installation"]
        installation_id = installation["id"]
    except (KeyError, TypeError) as error:
        raise ValueError("GitHub installation webhook is malformed") from error
    _positive_identifier(installation_id, field_name="installation id")
    if not isinstance(action, str) or not EVENT_NAME_PATTERN.fullmatch(action):
        raise ValueError("GitHub installation webhook is malformed")

    with conn.cursor() as cursor:
        if action == "created":
            try:
                account = installation["account"]
                account_id = account["id"]
                account_login = account["login"]
                account_type = account["type"]
                sender = payload["sender"]
                sender_id = sender["id"]
                sender_login = sender["login"]
            except (KeyError, TypeError) as error:
                raise ValueError("GitHub installation webhook is malformed") from error
            _positive_identifier(account_id, field_name="account id")
            _positive_identifier(sender_id, field_name="sender id")
            if (
                account_type not in {"Organization", "User"}
                or not isinstance(account_login, str)
                or not 1 <= len(account_login) <= 255
                or not isinstance(sender_login, str)
                or not 1 <= len(sender_login) <= 255
                or any("\x00" in value for value in (account_login, sender_login))
            ):
                raise ValueError("GitHub installation webhook is malformed")
            cursor.execute(
                """
                INSERT INTO relay_github_installations (
                    github_installation_id,
                    account_id,
                    account_login,
                    account_type,
                    installed_by_github_user_id,
                    installed_by_login
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (github_installation_id) DO UPDATE
                SET account_id = EXCLUDED.account_id,
                    account_login = EXCLUDED.account_login,
                    account_type = EXCLUDED.account_type,
                    installed_by_github_user_id = EXCLUDED.installed_by_github_user_id,
                    installed_by_login = EXCLUDED.installed_by_login,
                    status = 'active',
                    revoked_at = NULL,
                    updated_at = now()
                """,
                (
                    installation_id,
                    account_id,
                    account_login,
                    account_type,
                    sender_id,
                    sender_login,
                ),
            )
            return
        if action == "unsuspend":
            cursor.execute(
                """
                UPDATE relay_github_installations
                SET status = 'active',
                    revoked_at = NULL,
                    updated_at = now()
                WHERE github_installation_id = %s
                  AND status = 'suspended'
                """,
                (installation_id,),
            )
            if cursor.rowcount:
                cursor.execute(
                    """
                    UPDATE relay_nodes
                    SET revoked_at = NULL,
                        updated_at = now()
                    WHERE github_installation_id = %s
                      AND revoked_at IS NOT NULL
                    """,
                    (installation_id,),
                )
            return
        if action not in {"deleted", "suspend"}:
            # GitHub may add installation lifecycle actions. The signed event
            # still belongs in the delivery queue, but only actions with a
            # defined credential consequence change routing state.
            return
        status = "revoked" if action == "deleted" else "suspended"
        cursor.execute(
            """
            UPDATE relay_github_installations
            SET status = %s,
                revoked_at = now(),
                updated_at = now()
            WHERE github_installation_id = %s
            """,
            (status, installation_id),
        )
        cursor.execute(
            """
            UPDATE relay_nodes
            SET revoked_at = now(),
                updated_at = now()
            WHERE github_installation_id = %s
              AND revoked_at IS NULL
            """,
            (installation_id,),
        )


def authorize_installation_user(
    conn,
    *,
    user_id: int,
    github_installation_id: int,
) -> str:
    user = _positive_identifier(user_id, field_name="user_id")
    installation = _positive_identifier(
        github_installation_id,
        field_name="github_installation_id",
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT installation.account_login
            FROM relay_github_installations AS installation
            JOIN users
              ON users.github_user_id = installation.installed_by_github_user_id
            WHERE installation.github_installation_id = %s
              AND installation.status = 'active'
              AND users.id = %s
            """,
            (installation, user),
        )
        row = cursor.fetchone()
    if row is None:
        raise InstallationOwnershipError(
            "Installation is not attributed to the signed-in GitHub user"
        )
    return row["account_login"]


def exchange_pairing_code(
    conn,
    *,
    code: str,
    node_name: str,
) -> tuple[RelayNode, str]:
    try:
        digest = pairing_code_sha256(code)
    except ValueError as error:
        raise InvalidPairingCodeError("Pairing code is invalid or expired") from error
    name = validate_node_name(node_name)
    token = generate_node_token()
    token_digest = node_token_sha256(token)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE relay_pairing_codes
            SET consumed_at = now()
            WHERE code_sha256 = %s
              AND consumed_at IS NULL
              AND expires_at > now()
            RETURNING user_id, github_installation_id, code_sha256
            """,
            (digest,),
        )
        pairing = cursor.fetchone()
        if pairing is None or not secrets.compare_digest(pairing["code_sha256"], digest):
            raise InvalidPairingCodeError("Pairing code is invalid or expired")

        cursor.execute(
            """
            INSERT INTO relay_nodes (
                user_id,
                github_installation_id,
                name,
                token_sha256
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (github_installation_id) DO UPDATE
            SET user_id = EXCLUDED.user_id,
                name = EXCLUDED.name,
                token_sha256 = EXCLUDED.token_sha256,
                last_seen_at = NULL,
                revoked_at = NULL,
                updated_at = now()
            RETURNING
                id,
                user_id,
                github_installation_id,
                name,
                last_seen_at
            """,
            (
                int(pairing["user_id"]),
                int(pairing["github_installation_id"]),
                name,
                token_digest,
            ),
        )
        return _node_from_row(cursor.fetchone()), token


def authenticate_node(conn, *, token: str) -> RelayNode | None:
    try:
        digest = node_token_sha256(token)
    except ValueError:
        return None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                user_id,
                github_installation_id,
                name,
                last_seen_at,
                token_sha256
            FROM relay_nodes
            WHERE token_sha256 = %s
              AND revoked_at IS NULL
            FOR UPDATE
            """,
            (digest,),
        )
        row = cursor.fetchone()
        if row is None or not secrets.compare_digest(row["token_sha256"], digest):
            return None
        cursor.execute(
            """
            UPDATE relay_nodes
            SET last_seen_at = now(),
                updated_at = now()
            WHERE id = %s
              AND (
                    last_seen_at IS NULL
                    OR last_seen_at < now() - interval '5 minutes'
                  )
            """,
            (int(row["id"]),),
        )
        return _node_from_row(row)


def record_github_delivery(
    conn,
    *,
    github_installation_id: int,
    provider_delivery_id: str,
    event_name: str,
    payload: bytes,
) -> bool:
    installation = _positive_identifier(
        github_installation_id,
        field_name="github_installation_id",
    )
    if not isinstance(provider_delivery_id, str) or not DELIVERY_ID_PATTERN.fullmatch(
        provider_delivery_id
    ):
        raise ValueError("GitHub delivery id is invalid")
    if not isinstance(event_name, str) or not EVENT_NAME_PATTERN.fullmatch(event_name):
        raise ValueError("GitHub event name is invalid")
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_DELIVERY_BYTES:
        raise ValueError(
            f"GitHub delivery body must contain 1 to {MAX_DELIVERY_BYTES} bytes"
        )
    payload_digest = hashlib.sha256(payload).hexdigest()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO relay_deliveries (
                provider,
                provider_delivery_id,
                event_name,
                github_installation_id,
                payload,
                payload_sha256
            )
            VALUES ('github', %s, %s, %s, %s, %s)
            ON CONFLICT (provider, provider_delivery_id) DO NOTHING
            RETURNING id
            """,
            (
                provider_delivery_id,
                event_name,
                installation,
                payload,
                payload_digest,
            ),
        )
        if cursor.fetchone() is not None:
            return True
        cursor.execute(
            """
            SELECT
                event_name,
                github_installation_id,
                payload_sha256
            FROM relay_deliveries
            WHERE provider = 'github'
              AND provider_delivery_id = %s
            """,
            (provider_delivery_id,),
        )
        existing = cursor.fetchone()
    if (
        existing is None
        or existing["event_name"] != event_name
        or int(existing["github_installation_id"]) != installation
        or not secrets.compare_digest(existing["payload_sha256"], payload_digest)
    ):
        raise RelayDeliveryConflictError(
            "GitHub delivery id conflicts with previously accepted content"
        )
    return False


def lease_next_delivery(
    conn,
    *,
    node: RelayNode,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeasedRelayDelivery | None:
    lease = _bounded_seconds(
        lease_seconds,
        field_name="Relay delivery lease",
        maximum=MAX_LEASE_SECONDS,
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE relay_deliveries
            SET status = 'dead',
                leased_until = NULL,
                last_error_code = 'attempts_exhausted',
                updated_at = now()
            WHERE provider = 'github'
              AND github_installation_id = %s
              AND status = 'leased'
              AND leased_until < now()
              AND attempt_count >= %s
            """,
            (node.github_installation_id, MAX_DELIVERY_ATTEMPTS),
        )
        cursor.execute(
            """
            WITH candidate AS (
                SELECT id
                FROM relay_deliveries
                WHERE provider = 'github'
                  AND github_installation_id = %s
                  AND payload IS NOT NULL
                  AND (
                        status = 'queued'
                        OR (status = 'leased' AND leased_until < now())
                      )
                ORDER BY received_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE relay_deliveries AS delivery
            SET status = 'leased',
                leased_by_node_id = %s,
                leased_until = now() + make_interval(secs => %s),
                attempt_count = delivery.attempt_count + 1,
                updated_at = now()
            FROM candidate
            WHERE delivery.id = candidate.id
            RETURNING
                delivery.id,
                delivery.provider,
                delivery.provider_delivery_id,
                delivery.event_name,
                delivery.payload,
                delivery.payload_sha256,
                delivery.attempt_count,
                delivery.leased_until
            """,
            (node.github_installation_id, node.id, lease),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return LeasedRelayDelivery(
        id=int(row["id"]),
        provider=row["provider"],
        provider_delivery_id=row["provider_delivery_id"],
        event_name=row["event_name"],
        payload=bytes(row["payload"]),
        payload_sha256=row["payload_sha256"],
        attempt_count=int(row["attempt_count"]),
        leased_until=row["leased_until"],
    )


def acknowledge_delivery(
    conn,
    *,
    node: RelayNode,
    delivery_id: int,
) -> bool:
    identifier = _positive_identifier(delivery_id, field_name="delivery_id")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE relay_deliveries
            SET status = 'delivered',
                payload = NULL,
                leased_until = NULL,
                delivered_at = now(),
                updated_at = now()
            WHERE id = %s
              AND provider = 'github'
              AND github_installation_id = %s
              AND leased_by_node_id = %s
              AND status = 'leased'
            RETURNING id
            """,
            (identifier, node.github_installation_id, node.id),
        )
        if cursor.fetchone() is not None:
            return True
        cursor.execute(
            """
            SELECT status, leased_by_node_id, github_installation_id
            FROM relay_deliveries
            WHERE id = %s
              AND provider = 'github'
            """,
            (identifier,),
        )
        row = cursor.fetchone()
    return bool(
        row
        and row["status"] == "delivered"
        and int(row["leased_by_node_id"]) == node.id
        and int(row["github_installation_id"]) == node.github_installation_id
    )
