import os
import threading
import time
import uuid

import psycopg2

from service.oauth_store import (
    APP_INSTALL_PURPOSE,
    GITHUB_INSTALL_AUTH_PURPOSE,
    MAX_EXPIRED_STATES_PER_PURGE,
    consume_oauth_state,
    create_install_auth_state,
    create_install_state,
    generate_state,
    load_oauth_state,
    record_user_installation,
    state_sha256,
    upsert_user,
)


def test_install_state_can_be_inspected_before_single_use_claim():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    state = generate_state()
    user_id = None
    try:
        with connection:
            user = upsert_user(
                connection,
                github_user_id=_github_user_id(),
                login=f"retry-user-{uuid.uuid4().hex[:12]}",
                avatar_url=None,
            )
            user_id = user.id
            create_install_state(connection, state=state, user_id=user.id)

        pending = load_oauth_state(
            connection,
            state=state,
            purpose=APP_INSTALL_PURPOSE,
        )
        connection.commit()
        assert pending is not None
        assert pending.user_id == user_id

        claimed = consume_oauth_state(
            connection,
            state=state,
            purpose=APP_INSTALL_PURPOSE,
        )
        connection.commit()
        assert claimed is not None
        assert claimed.user_id == user_id
        assert (
            load_oauth_state(
                connection,
                state=state,
                purpose=APP_INSTALL_PURPOSE,
            )
            is None
        )
    finally:
        connection.close()


def _github_user_id() -> int:
    return int(uuid.uuid4().int % 1_000_000_000) + 1


def test_install_auth_state_is_single_use():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    state = generate_state()
    try:
        create_install_auth_state(connection, state=state)
        connection.commit()

        claimed = consume_oauth_state(
            connection,
            state=state,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
        )
        connection.commit()
        assert claimed is not None
        assert claimed.user_id is None

        replayed = consume_oauth_state(
            connection,
            state=state,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
        )
        connection.commit()
        assert replayed is None
    finally:
        connection.close()


def test_unknown_expired_and_cross_purpose_states_are_all_rejected():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    state = generate_state()
    try:
        assert (
            consume_oauth_state(
                connection,
                state=generate_state(),
                purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            )
            is None
        )
        # A malformed nonce must be rejected, not raise, so the route can render
        # the same page for every kind of bad state.
        assert (
            consume_oauth_state(
                connection,
                state="short",
                purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            )
            is None
        )

        create_install_auth_state(connection, state=state)
        connection.commit()
        assert (
            consume_oauth_state(
                connection,
                state=state,
                purpose=APP_INSTALL_PURPOSE,
            )
            is None
        )
        connection.commit()

        # `CHECK (expires_at > created_at)` forbids backdating the expiry alone,
        # so age the whole row the way the passage of time would.
        with connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE oauth_states
                SET created_at = now() - interval '2 hours',
                    expires_at = now() - interval '1 hour'
                WHERE state_sha256 = %s
                """,
                (state_sha256(state),),
            )
        assert (
            consume_oauth_state(
                connection,
                state=state,
                purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            )
            is None
        )
    finally:
        connection.close()


def test_only_one_of_two_concurrent_callbacks_can_claim_a_state():
    url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    first, second = psycopg2.connect(url), psycopg2.connect(url)
    state = generate_state()
    claimed: dict[str, object] = {}
    try:
        create_install_auth_state(first, state=state)
        first.commit()

        winner = consume_oauth_state(
            first,
            state=state,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
        )

        def race():
            claimed["loser"] = consume_oauth_state(
                second,
                state=state,
                purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            )

        contender = threading.Thread(target=race)
        contender.start()
        # The contender must block on the row lock rather than claim it too.
        time.sleep(0.5)
        assert contender.is_alive()

        first.commit()
        contender.join(timeout=10)
        assert not contender.is_alive()

        assert winner is not None
        assert claimed["loser"] is None
    finally:
        first.close()
        second.close()


def test_expired_states_are_drained_in_bounded_batches():
    """`/auth/github` is anonymous, so its per-request work must be capped."""
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    backlog = MAX_EXPIRED_STATES_PER_PURGE * 2
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM oauth_states")
            cursor.execute(
                """
                INSERT INTO oauth_states (
                    state_sha256, purpose, expires_at, consumed_at, created_at
                )
                SELECT encode(sha256(g::text::bytea), 'hex'), 'github_install_auth',
                       now() - interval '1 hour',
                       CASE WHEN g %% 2 = 0 THEN now() - interval '1 hour' END,
                       now() - interval '2 hours'
                FROM generate_series(1, %s) g
                """,
                (backlog,),
            )

        create_install_auth_state(connection, state=generate_state())
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM oauth_states")
            remaining = cursor.fetchone()[0]

        # One request drained exactly one batch — not the whole backlog, and not
        # nothing. The +1 is the row this request inserted.
        assert remaining == backlog - MAX_EXPIRED_STATES_PER_PURGE + 1

        # Consumed and abandoned states are both drained; retention is the TTL,
        # so nothing survives on a grace period.
        for _ in range(3):
            create_install_auth_state(connection, state=generate_state())
            connection.commit()
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM oauth_states WHERE expires_at < now()")
            assert cursor.fetchone()[0] == 0
    finally:
        with connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM oauth_states")
        connection.close()


def test_user_upsert_is_keyed_on_the_github_id_and_refreshes_the_profile():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    github_user_id = _github_user_id()
    try:
        first = upsert_user(
            connection,
            github_user_id=github_user_id,
            login="octocat",
            avatar_url="https://avatars.example.com/a.png",
        )
        connection.commit()
        renamed = upsert_user(
            connection,
            github_user_id=github_user_id,
            login="octocat-renamed",
            avatar_url=None,
        )
        connection.commit()

        assert renamed.id == first.id
        assert renamed.login == "octocat-renamed"
        assert renamed.avatar_url is None
    finally:
        connection.close()


def test_install_state_binds_a_user_and_installations_are_idempotent():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    state = generate_state()
    installation_id = _github_user_id()
    try:
        user = upsert_user(
            connection,
            github_user_id=_github_user_id(),
            login="octocat",
            avatar_url=None,
        )
        create_install_state(connection, state=state, user_id=user.id)
        connection.commit()

        claimed = consume_oauth_state(
            connection,
            state=state,
            purpose=APP_INSTALL_PURPOSE,
        )
        connection.commit()
        assert claimed is not None
        assert claimed.user_id == user.id

        record_user_installation(
            connection,
            user_id=user.id,
            github_installation_id=installation_id,
            account_login="acme",
        )
        # A repeated setup redirect must not fail on the composite primary key.
        record_user_installation(
            connection,
            user_id=user.id,
            github_installation_id=installation_id,
        )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT account_login
                FROM user_installations
                WHERE user_id = %s AND github_installation_id = %s
                """,
                (user.id, installation_id),
            )
            rows = cursor.fetchall()
        assert rows == [("acme",)]
    finally:
        connection.close()


def test_a_user_may_connect_several_installations():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    try:
        user = upsert_user(
            connection,
            github_user_id=_github_user_id(),
            login="octocat",
            avatar_url=None,
        )
        first, second = _github_user_id(), _github_user_id() + 1
        record_user_installation(
            connection,
            user_id=user.id,
            github_installation_id=first,
        )
        record_user_installation(
            connection,
            user_id=user.id,
            github_installation_id=second,
        )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM user_installations WHERE user_id = %s",
                (user.id,),
            )
            assert cursor.fetchone()[0] == 2
    finally:
        connection.close()
