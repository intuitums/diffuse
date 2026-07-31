"""Single-use OAuth state, GitHub identities, and Diffuse session tokens.

Diffuse is the confidential OAuth client for the CLI sign-in flow. This module
owns everything that touches the database for it: the TTL-bounded CSRF ledger,
the `users` upsert, the hashed session tokens, and the loopback redirect guard.
Session tokens are returned to the caller exactly once and only ever stored as
SHA-256, so a database read cannot recover a usable credential.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import psycopg2.extras

CLI_LOGIN_PURPOSE = "cli_login"
APP_INSTALL_PURPOSE = "app_install"
OAUTH_STATE_PURPOSES = frozenset({CLI_LOGIN_PURPOSE, APP_INSTALL_PURPOSE})

# The CLI listens on an unprivileged loopback port; anything below 1024 could
# only be bound by root and is far more likely to be a forged redirect target.
MIN_CALLBACK_PORT = 1024
MAX_CALLBACK_PORT = 65535
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

DEFAULT_OAUTH_STATE_TTL_SECONDS = 600
DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_OAUTH_STATE_TTL_SECONDS = 3600
MAX_SESSION_TTL_SECONDS = 365 * 24 * 60 * 60

# The CLI supplies its own nonce, so the shape is enforced here rather than
# trusted: 32 characters of URL-safe alphabet is the floor for 32 random bytes.
MIN_STATE_CHARS = 32
MAX_STATE_CHARS = 128
STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

MIN_SESSION_TOKEN_CHARS = 32
MAX_SESSION_TOKEN_CHARS = 512

MAX_GITHUB_ID = 2**63 - 1
GITHUB_LOGIN_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,253}[A-Za-z0-9])?")
MAX_AVATAR_URL_CHARS = 2048

# Expired states are pruned opportunistically rather than by a scheduled job.
# The batch is bounded because `/auth/cli` is unauthenticated: an unbounded
# DELETE would make every anonymous request do work proportional to the table,
# which an attacker could grow. A backlog drains across subsequent requests,
# and faster the harder the endpoint is hit.
MAX_EXPIRED_STATES_PER_PURGE = 200


@dataclass(frozen=True)
class ConsumedOAuthState:
    id: int
    purpose: str
    callback_port: int | None
    user_id: int | None


@dataclass(frozen=True)
class UserRecord:
    id: int
    github_user_id: int
    login: str
    avatar_url: str | None


@dataclass(frozen=True)
class SessionRecord:
    id: int
    user_id: int
    expires_at: datetime


def generate_state() -> str:
    """Mint a state nonce for a Diffuse-initiated authorization."""
    return secrets.token_urlsafe(32)


def generate_session_token() -> str:
    """Mint a 32-byte CSPRNG session token. The caller sees it exactly once."""
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


def validate_session_token(value: str) -> str:
    if (
        not MIN_SESSION_TOKEN_CHARS <= len(value) <= MAX_SESSION_TOKEN_CHARS
        or not value.isascii()
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError(
            f"Session tokens must contain {MIN_SESSION_TOKEN_CHARS} to "
            f"{MAX_SESSION_TOKEN_CHARS} visible ASCII characters"
        )
    return value


def state_sha256(value: str) -> str:
    return hashlib.sha256(validate_state(value).encode()).hexdigest()


def session_token_sha256(value: str) -> str:
    return hashlib.sha256(validate_session_token(value).encode()).hexdigest()


def validate_callback_port(value: object) -> int:
    """Accept only an unprivileged port a loopback listener could hold."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Callback port must be an integer")
    if not MIN_CALLBACK_PORT <= value <= MAX_CALLBACK_PORT:
        raise ValueError(
            f"Callback port must be between {MIN_CALLBACK_PORT} and "
            f"{MAX_CALLBACK_PORT}"
        )
    return value


def parse_callback_port(value: str) -> int:
    """Parse a `port` query parameter without letting `int()` be lenient."""
    if not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError("Callback port must be a decimal number")
    return validate_callback_port(int(value))


def build_loopback_redirect(*, port: int, token: str, host: str = "127.0.0.1") -> str:
    """Build the CLI callback URL, refusing any non-loopback target.

    The host is never taken from the request, so this is defence in depth rather
    than the only guard — but it keeps the open-redirect rule in one place and
    testable.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError("CLI callbacks may only target 127.0.0.1 or localhost")
    query = urlencode({"token": validate_session_token(token)})
    return f"http://{host}:{validate_callback_port(port)}/callback?{query}"


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


def session_ttl_seconds() -> int:
    return _ttl_seconds(
        "DIFFUSE_SESSION_TTL_SECONDS",
        default=DEFAULT_SESSION_TTL_SECONDS,
        maximum=MAX_SESSION_TTL_SECONDS,
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


def create_login_state(conn, *, state: str, callback_port: int | None) -> None:
    """Record a pending CLI sign-in. A missing port means browser-initiated."""
    state_hash = state_sha256(state)
    port = None if callback_port is None else validate_callback_port(callback_port)
    ttl = oauth_state_ttl_seconds()
    with conn.cursor() as cursor:
        _purge_expired_states(cursor)
        cursor.execute(
            """
            INSERT INTO oauth_states (
                state_sha256,
                purpose,
                callback_port,
                expires_at
            )
            VALUES (%s, %s, %s, now() + make_interval(secs => %s))
            """,
            (state_hash, CLI_LOGIN_PURPOSE, port, ttl),
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
            RETURNING id, purpose, callback_port, user_id, state_sha256
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
        callback_port=(
            None if row["callback_port"] is None else int(row["callback_port"])
        ),
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


def create_session(conn, *, user_id: int, token: str) -> SessionRecord:
    """Store the SHA-256 of a freshly minted session token."""
    token_hash = session_token_sha256(token)
    ttl = session_ttl_seconds()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO sessions (user_id, token_sha256, expires_at)
            VALUES (%s, %s, now() + make_interval(secs => %s))
            RETURNING id, user_id, expires_at
            """,
            (user_id, token_hash, ttl),
        )
        row = cursor.fetchone()
    return SessionRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        expires_at=row["expires_at"],
    )


def load_session(conn, *, token_sha256: str) -> SessionRecord | None:
    """Resolve a live session by token hash, refreshing `last_used_at`."""
    if not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
        raise ValueError("Session-token hash is invalid")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, user_id, expires_at
            FROM sessions
            WHERE token_sha256 = %s
              AND revoked_at IS NULL
              AND expires_at > now()
            FOR UPDATE
            """,
            (token_sha256,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            UPDATE sessions
            SET last_used_at = now(),
                updated_at = now()
            WHERE id = %s
              AND (
                    last_used_at IS NULL
                    OR last_used_at < now() - interval '5 minutes'
                  )
            """,
            (int(row["id"]),),
        )
    return SessionRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        expires_at=row["expires_at"],
    )


def revoke_session(conn, *, session_id: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sessions
            SET revoked_at = now(),
                updated_at = now()
            WHERE id = %s
              AND revoked_at IS NULL
            """,
            (session_id,),
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
