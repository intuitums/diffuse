"""Single-use GitHub installation-auth state and installer identities.

The hosted relay is the confidential GitHub OAuth client for GitHub App
installation and node pairing. It persists only TTL-bounded CSRF state and the
GitHub identity needed to attribute an installation. GitHub access tokens are
used for the identity lookup and are never persisted.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass

import psycopg2.extras

GITHUB_INSTALL_AUTH_PURPOSE = "github_install_auth"
APP_INSTALL_PURPOSE = "app_install"
OAUTH_STATE_PURPOSES = frozenset(
    {GITHUB_INSTALL_AUTH_PURPOSE, APP_INSTALL_PURPOSE}
)

DEFAULT_OAUTH_STATE_TTL_SECONDS = 600
MAX_OAUTH_STATE_TTL_SECONDS = 3600

# Thirty-two characters of URL-safe alphabet is the floor for state minted
# from 32 random bytes.
MIN_STATE_CHARS = 32
MAX_STATE_CHARS = 128
STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

MAX_GITHUB_ID = 2**63 - 1
GITHUB_LOGIN_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,253}[A-Za-z0-9])?")
MAX_AVATAR_URL_CHARS = 2048

# Expired states are pruned opportunistically rather than by a scheduled job.
# The batch is bounded because `/auth/github` is unauthenticated: an unbounded
# DELETE would make every anonymous request do work proportional to the table,
# which an attacker could grow. A backlog drains across subsequent requests,
# and faster the harder the endpoint is hit.
MAX_EXPIRED_STATES_PER_PURGE = 200


@dataclass(frozen=True)
class ConsumedOAuthState:
    id: int
    purpose: str
    user_id: int | None


@dataclass(frozen=True)
class UserRecord:
    id: int
    github_user_id: int
    login: str
    avatar_url: str | None


def generate_state() -> str:
    """Mint a state nonce for a Diffuse-initiated authorization."""
    return secrets.token_urlsafe(32)


def validate_state(value: str) -> str:
    if (
        not MIN_STATE_CHARS <= len(value) <= MAX_STATE_CHARS
        or not STATE_PATTERN.fullmatch(value)
    ):
        raise ValueError(
            f"OAuth state must contain {MIN_STATE_CHARS} to {MAX_STATE_CHARS} "
            "URL-safe characters"
        )
    return value


def state_sha256(value: str) -> str:
    return hashlib.sha256(validate_state(value).encode()).hexdigest()


def _ttl_seconds(variable_name: str, *, default: int, maximum: int) -> int:
    raw = os.environ.get(variable_name, "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise ValueError(f"{variable_name} must be a positive number of seconds")
    seconds = int(raw)
    if not 1 <= seconds <= maximum:
        raise ValueError(f"{variable_name} must be between 1 and {maximum} seconds")
    return seconds


def oauth_state_ttl_seconds() -> int:
    return _ttl_seconds(
        "DIFFUSE_OAUTH_STATE_TTL_SECONDS",
        default=DEFAULT_OAUTH_STATE_TTL_SECONDS,
        maximum=MAX_OAUTH_STATE_TTL_SECONDS,
    )


def _github_id(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if not 1 <= value <= MAX_GITHUB_ID:
        raise ValueError(f"{field_name} must be a positive GitHub identifier")
    return value


def parse_installation_id(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value):
        raise ValueError("installation_id must be a decimal number")
    return _github_id(int(value), field_name="installation_id")


def _login(value: object, *, field_name: str = "GitHub login") -> str:
    if not isinstance(value, str) or not GITHUB_LOGIN_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")
    return value


def _avatar_url(value: object) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > MAX_AVATAR_URL_CHARS
        or not value.startswith(("http://", "https://"))
        or any(character in value for character in "\r\n")
    ):
        raise ValueError("GitHub avatar URL is invalid")
    return value


def _purge_expired_states(cursor) -> None:
    """Drain a bounded batch of expired states. Retention is the TTL itself.

    `SKIP LOCKED` keeps concurrent sign-ins from queueing behind each other on
    the same batch, matching how the job queue claims rows.
    """
    cursor.execute(
        """
        DELETE FROM oauth_states
        WHERE id IN (
            SELECT id
            FROM oauth_states
            WHERE expires_at < now()
            ORDER BY expires_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,
        (MAX_EXPIRED_STATES_PER_PURGE,),
    )


def create_install_auth_state(conn, *, state: str) -> None:
    """Record a pending browser authorization for GitHub App installation."""
    state_hash = state_sha256(state)
    ttl = oauth_state_ttl_seconds()
    with conn.cursor() as cursor:
        _purge_expired_states(cursor)
        cursor.execute(
            """
            INSERT INTO oauth_states (
                state_sha256,
                purpose,
                expires_at
            )
            VALUES (%s, %s, now() + make_interval(secs => %s))
            """,
            (state_hash, GITHUB_INSTALL_AUTH_PURPOSE, ttl),
        )


def create_install_state(conn, *, state: str, user_id: int) -> None:
    """Record a pending GitHub App install already bound to a signed-in user."""
    state_hash = state_sha256(state)
    ttl = oauth_state_ttl_seconds()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO oauth_states (
                state_sha256,
                purpose,
                user_id,
                expires_at
            )
            VALUES (%s, %s, %s, now() + make_interval(secs => %s))
            """,
            (state_hash, APP_INSTALL_PURPOSE, user_id, ttl),
        )


def load_oauth_state(
    conn,
    *,
    state: str,
    purpose: str,
) -> ConsumedOAuthState | None:
    """Read a live state without consuming it.

    The App setup callback uses this only to learn the bound user before it
    verifies installation ownership. It still calls ``consume_oauth_state`` in
    the transaction that creates the pairing code, preserving single use while
    allowing a retry if GitHub's signed installation webhook arrives late.
    """
    if purpose not in OAUTH_STATE_PURPOSES:
        raise ValueError("OAuth state purpose is invalid")
    try:
        state_hash = state_sha256(state)
    except ValueError:
        return None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, purpose, user_id, state_sha256
            FROM oauth_states
            WHERE state_sha256 = %s
              AND purpose = %s
              AND consumed_at IS NULL
              AND expires_at > now()
            """,
            (state_hash, purpose),
        )
        row = cursor.fetchone()
    if row is None or not secrets.compare_digest(row["state_sha256"], state_hash):
        return None
    return ConsumedOAuthState(
        id=int(row["id"]),
        purpose=row["purpose"],
        user_id=None if row["user_id"] is None else int(row["user_id"]),
    )


def consume_oauth_state(conn, *, state: str, purpose: str) -> ConsumedOAuthState | None:
    """Claim a pending state exactly once, or return None if it is not claimable.

    The conditional UPDATE is the single-use guarantee: a replayed state matches
    no row the second time, even under concurrent callbacks.
    """
    if purpose not in OAUTH_STATE_PURPOSES:
        raise ValueError("OAuth state purpose is invalid")
    try:
        state_hash = state_sha256(state)
    except ValueError:
        return None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE oauth_states
            SET consumed_at = now()
            WHERE state_sha256 = %s
              AND purpose = %s
              AND consumed_at IS NULL
              AND expires_at > now()
            RETURNING id, purpose, user_id, state_sha256
            """,
            (state_hash, purpose),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    # Redundant given the indexed lookup above, but keeps the comparison of a
    # caller-supplied secret constant time as the security review requires.
    if not secrets.compare_digest(row["state_sha256"], state_hash):
        return None
    return ConsumedOAuthState(
        id=int(row["id"]),
        purpose=row["purpose"],
        user_id=None if row["user_id"] is None else int(row["user_id"]),
    )


def upsert_user(
    conn,
    *,
    github_user_id: int,
    login: str,
    avatar_url: str | None,
) -> UserRecord:
    """Insert or refresh the GitHub identity behind a completed authorization."""
    identifier = _github_id(github_user_id, field_name="github_user_id")
    normalized_login = _login(login)
    normalized_avatar = _avatar_url(avatar_url)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO users (github_user_id, login, avatar_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (github_user_id) DO UPDATE
            SET login = EXCLUDED.login,
                avatar_url = EXCLUDED.avatar_url,
                updated_at = now()
            RETURNING id, github_user_id, login, avatar_url
            """,
            (identifier, normalized_login, normalized_avatar),
        )
        row = cursor.fetchone()
    return UserRecord(
        id=int(row["id"]),
        github_user_id=int(row["github_user_id"]),
        login=row["login"],
        avatar_url=row["avatar_url"],
    )


def record_user_installation(
    conn,
    *,
    user_id: int,
    github_installation_id: int,
    account_login: str | None = None,
) -> None:
    """Link a GitHub App installation to the user who completed the install."""
    installation_id = _github_id(
        github_installation_id,
        field_name="github_installation_id",
    )
    normalized_account = (
        None
        if account_login is None
        else _login(account_login, field_name="Installation account login")
    )
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO user_installations (
                user_id,
                github_installation_id,
                account_login
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, github_installation_id) DO UPDATE
            SET account_login = COALESCE(
                    EXCLUDED.account_login,
                    user_installations.account_login
                ),
                updated_at = now()
            """,
            (user_id, installation_id, normalized_account),
        )
