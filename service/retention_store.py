"""Time-based retention for Diffuse's append-only tables.

Every other DELETE in the codebase is scoped to a single entity that is being
replaced, so the event ledgers — audit events, webhook deliveries, pull request
lifecycle events, review feedback, workflow bookkeeping, and the pgvector
`code_chunks` rows behind retired index snapshots — grow without bound. This
module drains them on a schedule.

It follows `service.oauth_store._purge_expired_states`: every statement deletes
at most a fixed number of rows under `FOR UPDATE SKIP LOCKED`, so per-call cost
is constant and concurrent drains never queue behind each other. A backlog
simply takes more calls to clear.

None of these tables has an index on its timestamp column, and adding one needs
a migration. Each purge therefore walks the primary key — which is a BIGSERIAL
assigned in insertion order, so it is monotonic with the timestamp and the
oldest rows come first.

Two window shapes exist, and which one a table gets is a correctness decision,
not a tuning knob:

* Ledgers whose rows all eventually become deletable (audit events, webhook
  deliveries and receipts, lifecycle events, feedback, finished attempts) cap
  the walk to a window of ids starting at the current `min(id)`. That keeps a
  call which finds nothing to delete as cheap as one that finds a full batch.
  The anchor is safe here precisely because the row it names is itself
  deletable: draining the front of the table moves `min(id)` forward.
* Tables that permanently retain some rows — workflow jobs behind a published
  review, review runs, index snapshots kept for provenance — must NOT use that
  anchor. An undeletable row at `min(id)` pins the window forever, and once the
  window drains the purge selects the same rows and deletes nothing on every
  later pass. Those purges select purely by age instead, and where the deleted
  rows hang off a parent that is never deleted, the parent must still own a
  child to be a candidate. Both properties make the window advance on its own.

The age-based purges give up the constant-cost-when-idle property: a pass that
finds nothing walks that table's primary key once. That is the price of a drain
that cannot stall, it is bounded by one index walk per retention interval on a
background thread, and it is far cheaper than the unbounded growth it prevents.
"""

from __future__ import annotations

import logging
import os
import re

LOGGER = logging.getLogger(__name__)

# Deletes per statement. Small enough that a purge never holds locks or WAL
# pressure long enough to interfere with the review path.
MAX_ROWS_PER_PURGE = 1000

# How many of the oldest primary keys a purge may look at. Bounds the work of a
# call that deletes nothing; a retention backlog older than this window drains
# across successive calls rather than in one long transaction.
MAX_SCANNED_ROWS_PER_PURGE = 5000

# Retired index snapshots examined per code-chunk purge. Snapshots are created
# per index build, so this table is orders of magnitude smaller than the chunks
# it owns.
MAX_SNAPSHOTS_PER_PURGE = 8

MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650

# Defaults are deliberately generous: retention is destructive and an operator
# who has not thought about it should lose nothing they would plausibly still
# want. Compliance-flavoured trails (audit, feedback that trains the learning
# system) keep a year; operational churn keeps a quarter; embeddings behind
# snapshots that no review can select any more keep a month, because they are
# by far the most expensive rows to store.
DEFAULT_AUDIT_EVENT_RETENTION_DAYS = 365
DEFAULT_FEEDBACK_EVENT_RETENTION_DAYS = 365
DEFAULT_WEBHOOK_RETENTION_DAYS = 90
DEFAULT_WORKFLOW_RETENTION_DAYS = 90
DEFAULT_REVIEW_CONTEXT_RETENTION_DAYS = 90
DEFAULT_CODE_CHUNK_RETENTION_DAYS = 30

# A workflow job cascades to its review run, findings, and threads, so only
# jobs that never produced a review are collected here. Dropping published
# review history is a product decision an operator has to make explicitly, not
# something a default-on background drain should do.
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "dead", "cancelled", "superseded")

# Statuses a review can no longer leave. A run still generating or publishing
# must keep the context snapshot that describes what it retrieved.
TERMINAL_REVIEW_STATUSES = ("published", "skipped", "failed", "superseded")

# Snapshots in these states can never be selected by retrieval again: a repo
# has at most one `active` snapshot and `building` ones are still being filled.
RETIRED_SNAPSHOT_STATUSES = ("superseded", "failed")


TRUTHY_FLAG_VALUES = ("1", "true", "yes", "on")
FALSEY_FLAG_VALUES = ("0", "false", "no", "off")


def retention_enabled() -> bool:
    """Retention is on by default; an operator can freeze every table at once.

    A value Diffuse does not recognise is rejected rather than read as consent:
    this flag guards the only statements in the codebase that delete data an
    operator did not ask to delete, so `DIFFUSE_RETENTION_ENABLED=maybe` must
    not drain nine tables. Raising here fails closed — `purge_expired_records`
    propagates it before issuing a single statement — and matches how
    `service.webhook_server.api_docs_enabled` treats its own flag: only the
    documented spellings turn a guard off.
    """
    raw = os.environ.get("DIFFUSE_RETENTION_ENABLED", "").strip().lower()
    if not raw:
        return True
    if raw in TRUTHY_FLAG_VALUES:
        return True
    if raw in FALSEY_FLAG_VALUES:
        return False
    raise ValueError(
        "DIFFUSE_RETENTION_ENABLED must be one of "
        + ", ".join(TRUTHY_FLAG_VALUES + FALSEY_FLAG_VALUES)
    )


def _retention_days(variable_name: str, *, default: int) -> int:
    raw = os.environ.get(variable_name, "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,4}", raw):
        raise ValueError(f"{variable_name} must be a positive number of days")
    days = int(raw)
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        raise ValueError(
            f"{variable_name} must be between {MIN_RETENTION_DAYS} and "
            f"{MAX_RETENTION_DAYS} days"
        )
    return days


def audit_event_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_AUDIT_EVENT_RETENTION_DAYS",
        default=DEFAULT_AUDIT_EVENT_RETENTION_DAYS,
    )


def feedback_event_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_FEEDBACK_EVENT_RETENTION_DAYS",
        default=DEFAULT_FEEDBACK_EVENT_RETENTION_DAYS,
    )


def webhook_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_WEBHOOK_RETENTION_DAYS",
        default=DEFAULT_WEBHOOK_RETENTION_DAYS,
    )


def workflow_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_WORKFLOW_RETENTION_DAYS",
        default=DEFAULT_WORKFLOW_RETENTION_DAYS,
    )


def review_context_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_REVIEW_CONTEXT_RETENTION_DAYS",
        default=DEFAULT_REVIEW_CONTEXT_RETENTION_DAYS,
    )


def code_chunk_retention_days() -> int:
    return _retention_days(
        "DIFFUSE_CODE_CHUNK_RETENTION_DAYS",
        default=DEFAULT_CODE_CHUNK_RETENTION_DAYS,
    )


def _validate_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Retention batch limit must be an integer")
    if not 1 <= value <= MAX_ROWS_PER_PURGE:
        raise ValueError(
            f"Retention batch limit must be between 1 and {MAX_ROWS_PER_PURGE}"
        )
    return value


def _oldest_id(cursor, *, table: str) -> int | None:
    """Read the front of the primary key. `min()` on a PK is an index probe.

    Only valid as a window anchor for a table whose oldest row is always itself
    eventually deletable; see the module docstring.
    """
    cursor.execute(f"SELECT min(id) FROM {table}")
    row = cursor.fetchone()
    return None if row is None else row[0]


def _purge_by_age(
    cursor,
    *,
    table: str,
    timestamp_column: str,
    days: int,
    limit: int,
    extra_predicate: str = "",
) -> int:
    """Delete a bounded batch of the oldest deletable rows, by age alone.

    For tables that retain some rows forever. There is no id ceiling, so every
    row past the window is reachable no matter how much undeletable history
    sits in front of it; `ORDER BY id LIMIT %s` still walks the primary key
    oldest-first and stops as soon as it has a batch, so a backlog — which
    always sits at the front of the key — is found immediately.

    `table`, `timestamp_column` and `extra_predicate` are module constants,
    never caller input; only the interval and the batch size are bound.
    """
    cursor.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id
            FROM {table}
            WHERE {timestamp_column} < now() - make_interval(days => %s)
              {extra_predicate}
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,
        (days, limit),
    )
    return max(cursor.rowcount, 0)


def _purge_by_primary_key(
    cursor,
    *,
    table: str,
    timestamp_column: str,
    days: int,
    limit: int,
    extra_predicate: str = "",
) -> int:
    """Delete a bounded batch of the oldest rows past their retention window.

    `table`, `timestamp_column` and `extra_predicate` are module constants, never
    caller input; only the id window, the interval, and the batch size are bound
    as parameters.
    """
    oldest_id = _oldest_id(cursor, table=table)
    if oldest_id is None:
        return 0
    cursor.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id
            FROM {table}
            WHERE id < %s
              AND {timestamp_column} < now() - make_interval(days => %s)
              {extra_predicate}
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,
        (oldest_id + MAX_SCANNED_ROWS_PER_PURGE, days, limit),
    )
    return max(cursor.rowcount, 0)


def purge_audit_events(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    with conn.cursor() as cursor:
        return _purge_by_primary_key(
            cursor,
            table="audit_events",
            timestamp_column="occurred_at",
            days=audit_event_retention_days(),
            limit=_validate_limit(limit),
        )


def purge_review_feedback_events(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    with conn.cursor() as cursor:
        return _purge_by_primary_key(
            cursor,
            table="review_feedback_events",
            timestamp_column="observed_at",
            days=feedback_event_retention_days(),
            limit=_validate_limit(limit),
        )


def purge_pull_request_lifecycle_events(
    conn,
    *,
    limit: int = MAX_ROWS_PER_PURGE,
) -> int:
    with conn.cursor() as cursor:
        return _purge_by_primary_key(
            cursor,
            table="pull_request_lifecycle_events",
            timestamp_column="recorded_at",
            days=webhook_retention_days(),
            limit=_validate_limit(limit),
        )


def purge_webhook_deliveries(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    """Drop delivery receipts past the webhook window.

    A lifecycle event references its delivery `ON DELETE CASCADE`, which is why
    both share one window: purging a delivery would otherwise silently take a
    lifecycle event that had not reached its own cutoff yet.
    """
    with conn.cursor() as cursor:
        return _purge_by_primary_key(
            cursor,
            table="scm_webhook_deliveries",
            timestamp_column="received_at",
            days=webhook_retention_days(),
            limit=_validate_limit(limit),
        )


def purge_webhook_rejections(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    """Drop refusal receipts for deliveries Diffuse declined.

    Unlike the other ledgers this one is upserted in place — a repeated delivery
    bumps `attempts` and `last_seen_at` — so the primary key does not track
    recency and the dedicated `last_seen_at` index does the ordering instead.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM scm_webhook_rejections
            WHERE id IN (
                SELECT id
                FROM scm_webhook_rejections
                WHERE last_seen_at < now() - make_interval(days => %s)
                ORDER BY last_seen_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (webhook_retention_days(), _validate_limit(limit)),
        )
        return max(cursor.rowcount, 0)


def purge_workflow_attempts(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    """Drop finished attempt rows. Their job and review history are untouched."""
    with conn.cursor() as cursor:
        return _purge_by_primary_key(
            cursor,
            table="workflow_attempts",
            timestamp_column="finished_at",
            days=workflow_retention_days(),
            limit=_validate_limit(limit),
        )


def purge_workflow_jobs(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    """Drop settled queue rows that never produced a review run.

    Selected by age, not by an id window: a job behind a published review is
    kept forever, so the first such job to reach the front of the primary key
    would otherwise pin the window and freeze this drain permanently.
    """
    statuses = ", ".join(f"'{status}'" for status in TERMINAL_JOB_STATUSES)
    with conn.cursor() as cursor:
        return _purge_by_age(
            cursor,
            table="workflow_jobs",
            timestamp_column="completed_at",
            days=workflow_retention_days(),
            limit=_validate_limit(limit),
            extra_predicate=f"""
              AND status IN ({statuses})
              AND NOT EXISTS (
                  SELECT 1
                  FROM review_runs
                  WHERE review_runs.workflow_job_id = workflow_jobs.id
              )
            """,
        )


def purge_review_run_context_snapshots(
    conn,
    *,
    limit: int = MAX_ROWS_PER_PURGE,
) -> int:
    """Drop the retrieval provenance of settled review runs.

    The table has no timestamp of its own, so the bound comes from the parent
    run. A run carries at most seven snapshot rows, so limiting the parents also
    limits the deletes.

    Retention never deletes `review_runs`, so a candidate run must still own at
    least one snapshot row. Without that predicate the oldest settled runs stay
    candidates after their snapshots are gone, and every later pass re-selects
    the same drained runs and deletes nothing.
    """
    statuses = ", ".join(f"'{status}'" for status in TERMINAL_REVIEW_STATUSES)
    batch = _validate_limit(limit)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM review_run_context_snapshots
            WHERE review_run_id IN (
                SELECT id
                FROM review_runs
                WHERE status IN ({statuses})
                  AND updated_at < now() - make_interval(days => %s)
                  AND EXISTS (
                      SELECT 1
                      FROM review_run_context_snapshots AS owned
                      WHERE owned.review_run_id = review_runs.id
                  )
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (review_context_retention_days(), batch),
        )
        return max(cursor.rowcount, 0)


def purge_code_chunks(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> int:
    """Drop embeddings owned by index snapshots retrieval can never select.

    The snapshot rows themselves stay: `review_runs.index_snapshot_id` points at
    them, so deleting one would erase which index a published review read. Only
    the chunks — the rows carrying a 1536-dimension vector each — are reclaimed.

    Because the snapshot rows survive, a snapshot only stays a candidate while
    it still owns chunks. Without that predicate the same oldest retired
    snapshots are picked on every pass forever: the first pass empties them, and
    every pass after it deletes nothing while newer snapshots keep their
    embeddings for good — which would leave the largest table in the schema
    effectively unbounded.
    """
    statuses = ", ".join(f"'{status}'" for status in RETIRED_SNAPSHOT_STATUSES)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM code_chunks
            WHERE id IN (
                SELECT id
                FROM code_chunks
                WHERE snapshot_id IN (
                    SELECT id
                    FROM index_snapshots
                    WHERE status IN ({statuses})
                      AND updated_at < now() - make_interval(days => %s)
                      AND EXISTS (
                          SELECT 1
                          FROM code_chunks AS owned
                          WHERE owned.snapshot_id = index_snapshots.id
                      )
                    ORDER BY id
                    LIMIT %s
                )
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (
                code_chunk_retention_days(),
                MAX_SNAPSHOTS_PER_PURGE,
                _validate_limit(limit),
            ),
        )
        return max(cursor.rowcount, 0)


# Lifecycle events are purged before the deliveries they cascade from so a
# single pass never has to rely on the cascade, and chunks come last because
# they are the slowest statement.
RETENTION_PURGES = (
    ("audit_events", purge_audit_events),
    ("review_feedback_events", purge_review_feedback_events),
    ("pull_request_lifecycle_events", purge_pull_request_lifecycle_events),
    ("scm_webhook_deliveries", purge_webhook_deliveries),
    ("scm_webhook_rejections", purge_webhook_rejections),
    ("review_run_context_snapshots", purge_review_run_context_snapshots),
    ("workflow_attempts", purge_workflow_attempts),
    ("workflow_jobs", purge_workflow_jobs),
    ("code_chunks", purge_code_chunks),
)


def purge_expired_records(conn, *, limit: int = MAX_ROWS_PER_PURGE) -> dict[str, int]:
    """Run one bounded retention pass and report the rows removed per table.

    Every table is its own transaction. Nine deletes in one transaction would
    hold every lock until the last statement finished, and — because the caller
    can only log a failure — a single broken statement would roll the whole pass
    back and silently disable retention for all nine tables for the life of the
    deployment. Here a failure costs exactly its own table, is logged with the
    table that caused it, and the remaining purges still run and commit.
    """
    _validate_limit(limit)
    if not retention_enabled():
        return {}
    removed: dict[str, int] = {}
    for table, purge in RETENTION_PURGES:
        try:
            removed[table] = purge(conn, limit=limit)
        except Exception:
            conn.rollback()
            removed[table] = 0
            LOGGER.exception("Retention purge failed table=%s", table)
        else:
            conn.commit()
    return removed
