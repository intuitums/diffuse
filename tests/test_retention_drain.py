"""Executable proof that every retention window advances until it is empty.

The other retention tests assert on the shape of the SQL. These run it. A
purge whose drain window is pinned to a row nothing ever deletes looks entirely
correct statement-by-statement and still deletes zero rows on every pass after
the first, so the only test that catches it is one that calls a purge in a loop
and watches the table.

SQLite stands in for PostgreSQL: the three regressions covered here are about
which rows a window can reach, which is ordinary relational semantics, not
anything PostgreSQL-specific. `tests/integration/test_retention_postgres.py`
runs the same statements against the real schema.
"""

import re
import sqlite3

import pytest

from service.retention_store import (
    MAX_SCANNED_ROWS_PER_PURGE,
    MAX_SNAPSHOTS_PER_PURGE,
    purge_audit_events,
    purge_code_chunks,
    purge_review_run_context_snapshots,
    purge_workflow_jobs,
)

_AGE_EXPRESSION = re.compile(
    r"now\(\)\s*-\s*make_interval\(days\s*=>\s*%s\)",
)


def _translate(query: str) -> str:
    """Rewrite the PostgreSQL statement into the SQLite dialect.

    Only the two constructs SQLite lacks are touched, and the parameter order is
    preserved, so the predicates and the row limits under test are executed
    exactly as written.
    """
    translated = query.replace("FOR UPDATE SKIP LOCKED", "")
    translated = _AGE_EXPRESSION.sub(
        "datetime('now', '-' || ? || ' days')",
        translated,
    )
    return translated.replace("%s", "?")


class _Cursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._cursor.close()
        return None

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query, parameters=None):
        self._cursor.execute(_translate(query), tuple(parameters or ()))

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _Connection:
    """The psycopg2 surface `service.retention_store` actually uses."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")

    def cursor(self, **_kwargs):
        return _Cursor(self.connection.cursor())

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def run(self, statement, parameters=()):
        with self.connection:
            self.connection.execute(statement, parameters)

    def count(self, statement, parameters=()):
        return self.connection.execute(statement, parameters).fetchone()[0]


def _drain(purge, connection, *, limit, passes=25):
    """Call a purge until it stops deleting; report the rows removed per pass."""
    removed = []
    for _pass in range(passes):
        deleted = purge(connection, limit=limit)
        removed.append(deleted)
        if deleted == 0:
            break
    return removed


@pytest.fixture
def connection():
    database = _Connection()
    database.run(
        """
        CREATE TABLE index_snapshots (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE code_chunks (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE review_runs (
            id INTEGER PRIMARY KEY,
            workflow_job_id INTEGER,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE review_run_context_snapshots (
            review_run_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE review_conversation_messages (
            workflow_job_id INTEGER NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE suggested_rule_generation_runs (
            workflow_job_id INTEGER NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE workflow_jobs (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    database.run(
        """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY,
            occurred_at TEXT NOT NULL
        )
        """
    )
    return database


def test_code_chunks_drain_past_the_oldest_batch_of_retired_snapshots(connection):
    # Nothing in the codebase deletes an `index_snapshots` row: they are kept so
    # `review_runs.index_snapshot_id` still names the index a published review
    # read. A purge that always looks at the oldest MAX_SNAPSHOTS_PER_PURGE
    # retired snapshots therefore empties those and then re-selects the same
    # drained rows forever, and every snapshot behind them keeps its 1536-float
    # embeddings for the life of the deployment.
    snapshots = MAX_SNAPSHOTS_PER_PURGE + 3
    for snapshot_id in range(1, snapshots + 1):
        connection.run(
            """
            INSERT INTO index_snapshots (id, status, updated_at)
            VALUES (?, 'superseded', datetime('now', '-90 days'))
            """,
            (snapshot_id,),
        )
        connection.run(
            "INSERT INTO code_chunks (snapshot_id) VALUES (?)",
            (snapshot_id,),
        )

    removed = _drain(purge_code_chunks, connection, limit=4)

    assert sum(removed) == snapshots
    assert connection.count("SELECT count(*) FROM code_chunks") == 0
    # Provenance survives the drain; only the vectors are reclaimed.
    assert connection.count("SELECT count(*) FROM index_snapshots") == snapshots


def test_code_chunks_of_snapshots_retrieval_can_still_select_are_kept(connection):
    connection.run(
        """
        INSERT INTO index_snapshots (id, status, updated_at)
        VALUES (1, 'active', datetime('now', '-900 days'))
        """
    )
    connection.run("INSERT INTO code_chunks (snapshot_id) VALUES (1)")

    assert purge_code_chunks(connection, limit=10) == 0
    assert connection.count("SELECT count(*) FROM code_chunks") == 1


def test_context_snapshots_drain_past_the_oldest_settled_review_runs(connection):
    # `review_runs` is never deleted by retention, so neither the id window
    # anchored at its `min(id)` nor the set of oldest settled runs ever moves.
    # Run 6000 sits beyond that window; runs 1 and 2 stay candidates after their
    # provenance rows are gone.
    for run_id in (1, 2, MAX_SCANNED_ROWS_PER_PURGE + 1000):
        connection.run(
            """
            INSERT INTO review_runs (id, workflow_job_id, status, updated_at)
            VALUES (?, ?, 'published', datetime('now', '-180 days'))
            """,
            (run_id, run_id),
        )
        connection.run(
            """
            INSERT INTO review_run_context_snapshots (review_run_id, ordinal)
            VALUES (?, 1)
            """,
            (run_id,),
        )

    removed = _drain(purge_review_run_context_snapshots, connection, limit=2)

    assert sum(removed) == 3
    assert connection.count(
        "SELECT count(*) FROM review_run_context_snapshots"
    ) == 0
    assert connection.count("SELECT count(*) FROM review_runs") == 3


def test_context_snapshots_of_unsettled_review_runs_are_kept(connection):
    connection.run(
        """
        INSERT INTO review_runs (id, workflow_job_id, status, updated_at)
        VALUES (1, 1, 'generating', datetime('now', '-180 days'))
        """
    )
    connection.run(
        """
        INSERT INTO review_run_context_snapshots (review_run_id, ordinal)
        VALUES (1, 1)
        """
    )

    assert purge_review_run_context_snapshots(connection, limit=10) == 0
    assert connection.count("SELECT count(*) FROM review_run_context_snapshots") == 1


def test_workflow_jobs_drain_past_every_kind_of_retained_product_history(connection):
    # Jobs 1-3 own durable review, conversation, and rule-learning records, so
    # retention must never collect them. Anchoring the drain window at one of
    # those permanent rows would freeze it and leave the orphaned job behind.
    for job_id in (1, 2, 3):
        connection.run(
            """
            INSERT INTO workflow_jobs (id, status, completed_at)
            VALUES (?, 'succeeded', datetime('now', '-180 days'))
            """,
            (job_id,),
        )
    connection.run(
        """
        INSERT INTO review_runs (id, workflow_job_id, status, updated_at)
        VALUES (1, 1, 'published', datetime('now', '-180 days'))
        """
    )
    connection.run(
        "INSERT INTO review_conversation_messages (workflow_job_id) VALUES (2)"
    )
    connection.run(
        "INSERT INTO suggested_rule_generation_runs (workflow_job_id) VALUES (3)"
    )
    unreachable_id = MAX_SCANNED_ROWS_PER_PURGE + 1000
    connection.run(
        """
        INSERT INTO workflow_jobs (id, status, completed_at)
        VALUES (?, 'failed', datetime('now', '-180 days'))
        """,
        (unreachable_id,),
    )

    removed = _drain(purge_workflow_jobs, connection, limit=5)

    assert sum(removed) == 1
    assert connection.count("SELECT count(*) FROM workflow_jobs") == 3
    assert connection.count(
        "SELECT count(*) FROM workflow_jobs WHERE id IN (1, 2, 3)"
    ) == 3


def test_workflow_jobs_inside_their_window_or_still_running_are_kept(connection):
    connection.run(
        """
        INSERT INTO workflow_jobs (id, status, completed_at)
        VALUES (1, 'succeeded', datetime('now', '-1 days'))
        """
    )
    connection.run(
        "INSERT INTO workflow_jobs (id, status, completed_at) VALUES (2, 'running', NULL)"
    )

    assert purge_workflow_jobs(connection, limit=10) == 0
    assert connection.count("SELECT count(*) FROM workflow_jobs") == 2


def test_the_primary_key_window_still_drains_a_ledger_with_no_retained_rows(
    connection,
):
    # The six purges over tables whose rows all eventually become deletable keep
    # the cheap `min(id)` window; draining the front of the table moves the
    # anchor, so they must go on emptying in batches.
    for _row in range(5):
        connection.run(
            """
            INSERT INTO audit_events (occurred_at)
            VALUES (datetime('now', '-400 days'))
            """
        )

    removed = _drain(purge_audit_events, connection, limit=2)

    assert removed == [2, 2, 1, 0]
    assert connection.count("SELECT count(*) FROM audit_events") == 0
