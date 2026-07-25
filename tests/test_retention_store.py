import pytest

from service.retention_store import (
    DEFAULT_AUDIT_EVENT_RETENTION_DAYS,
    MAX_ROWS_PER_PURGE,
    MAX_SCANNED_ROWS_PER_PURGE,
    RETENTION_PURGES,
    audit_event_retention_days,
    code_chunk_retention_days,
    purge_audit_events,
    purge_code_chunks,
    purge_expired_records,
    purge_review_run_context_snapshots,
    purge_webhook_rejections,
    purge_workflow_jobs,
    retention_enabled,
    webhook_retention_days,
)


class _Cursor:
    def __init__(self, *, oldest_id=1, rowcount=0, failing_table=None):
        self.statements = []
        self._oldest_id = oldest_id
        self._failing_table = failing_table
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, parameters=None):
        normalized = " ".join(query.split())
        self.statements.append((normalized, parameters))
        if self._failing_table and self._failing_table in normalized:
            raise RuntimeError(f"deadlock detected on {self._failing_table}")

    def fetchone(self):
        return (self._oldest_id,)


class _Connection:
    def __init__(self, *, oldest_id=1, rowcount=0, failing_table=None):
        self.cursor_instance = _Cursor(
            oldest_id=oldest_id,
            rowcount=rowcount,
            failing_table=failing_table,
        )
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _delete_statements(connection):
    return [
        (query, parameters)
        for query, parameters in connection.cursor_instance.statements
        if query.startswith("DELETE")
    ]


def test_every_purge_is_bounded_and_skips_locked_rows():
    # An unbounded retention DELETE would make one call's cost proportional to
    # the whole table and would queue behind concurrent workers.
    for _table, purge in RETENTION_PURGES:
        connection = _Connection(rowcount=7)

        assert purge(connection) == 7

        deletes = _delete_statements(connection)
        assert len(deletes) == 1
        query, parameters = deletes[0]
        assert "LIMIT %s" in query
        assert "FOR UPDATE SKIP LOCKED" in query
        assert parameters[-1] == MAX_ROWS_PER_PURGE


def test_primary_key_walk_is_capped_to_a_window_of_the_oldest_rows():
    # Without the id ceiling a purge that finds nothing to delete would still
    # scan the entire primary key, because no retention column is indexed.
    connection = _Connection(oldest_id=4200)

    purge_audit_events(connection)

    minimum, delete = connection.cursor_instance.statements
    assert minimum[0] == "SELECT min(id) FROM audit_events"
    assert "id < %s" in delete[0]
    assert delete[1] == (
        4200 + MAX_SCANNED_ROWS_PER_PURGE,
        audit_event_retention_days(),
        MAX_ROWS_PER_PURGE,
    )


def test_empty_table_purges_nothing_without_issuing_a_delete():
    connection = _Connection(oldest_id=None)

    assert purge_audit_events(connection) == 0
    assert _delete_statements(connection) == []


def test_undefined_rowcount_is_not_reported_as_negative_rows():
    connection = _Connection(rowcount=-1)

    assert purge_audit_events(connection) == 0


def test_rejections_order_by_last_seen_at_because_the_key_is_reused():
    # Rejections are upserted in place, so id order does not track recency.
    connection = _Connection()

    purge_webhook_rejections(connection)

    query, parameters = _delete_statements(connection)[0]
    assert "ORDER BY last_seen_at" in query
    assert parameters == (webhook_retention_days(), MAX_ROWS_PER_PURGE)


def test_workflow_job_purge_never_cascades_into_review_history():
    connection = _Connection()

    purge_workflow_jobs(connection)

    query, _parameters = _delete_statements(connection)[0]
    assert "NOT EXISTS" in query
    assert "review_runs.workflow_job_id = workflow_jobs.id" in query
    assert "status IN ('succeeded', 'failed', 'dead', 'cancelled', 'superseded')" in (
        query
    )


def test_context_snapshot_purge_only_touches_settled_review_runs():
    connection = _Connection()

    purge_review_run_context_snapshots(connection)

    query, _parameters = _delete_statements(connection)[0]
    assert "status IN ('published', 'skipped', 'failed', 'superseded')" in query


def test_code_chunk_purge_is_restricted_to_snapshots_retrieval_cannot_select():
    connection = _Connection()

    purge_code_chunks(connection)

    query, parameters = _delete_statements(connection)[0]
    assert "FROM index_snapshots" in query
    assert "status IN ('superseded', 'failed')" in query
    # The snapshot rows themselves survive: review provenance points at them.
    assert "DELETE FROM code_chunks" in query
    assert parameters[0] == code_chunk_retention_days()


def test_retention_windows_are_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("DIFFUSE_AUDIT_EVENT_RETENTION_DAYS", " 30 ")
    assert audit_event_retention_days() == 30

    monkeypatch.setenv("DIFFUSE_AUDIT_EVENT_RETENTION_DAYS", "")
    assert audit_event_retention_days() == DEFAULT_AUDIT_EVENT_RETENTION_DAYS

    for invalid in ("0", "3651", "-1", "1.5", "thirty", "30 days"):
        monkeypatch.setenv("DIFFUSE_AUDIT_EVENT_RETENTION_DAYS", invalid)
        with pytest.raises(ValueError, match="DIFFUSE_AUDIT_EVENT_RETENTION_DAYS"):
            audit_event_retention_days()


def test_batch_limit_is_capped_so_a_caller_cannot_request_an_unbounded_delete():
    connection = _Connection()

    for invalid in (0, -1, MAX_ROWS_PER_PURGE + 1, True, 1.0, "10"):
        with pytest.raises(ValueError, match="Retention batch limit"):
            purge_audit_events(connection, limit=invalid)


def test_purges_over_tables_that_retain_rows_are_not_anchored_to_min_id():
    # `workflow_jobs` keeps every job behind a review run and `review_runs` is
    # never deleted at all, so a window anchored at `min(id)` is anchored to a
    # row that nothing will ever remove: it stops advancing the moment such a
    # row reaches the front of the key, and everything past the window becomes
    # permanently unreachable. Those two purges must select by age instead.
    for purge, table in (
        (purge_workflow_jobs, "workflow_jobs"),
        (purge_review_run_context_snapshots, "review_runs"),
    ):
        connection = _Connection()

        purge(connection)

        statements = connection.cursor_instance.statements
        assert not any(
            query.startswith("SELECT min(") for query, _parameters in statements
        ), table
        query, parameters = _delete_statements(connection)[0]
        assert "id < %s" not in query, table
        assert "make_interval" in query, table
        assert parameters[-1] == MAX_ROWS_PER_PURGE, table


def test_a_parent_is_only_a_candidate_while_it_still_owns_rows_to_delete():
    # Neither `index_snapshots` nor `review_runs` is ever deleted, so a purge
    # keyed only on the parent's status and age re-selects the same drained
    # parents on every pass and deletes nothing after the first one.
    connection = _Connection()
    purge_code_chunks(connection)
    query, _parameters = _delete_statements(connection)[0]
    assert "EXISTS ( SELECT 1 FROM code_chunks AS owned" in query
    assert "owned.snapshot_id = index_snapshots.id" in query

    connection = _Connection()
    purge_review_run_context_snapshots(connection)
    query, _parameters = _delete_statements(connection)[0]
    assert "EXISTS ( SELECT 1 FROM review_run_context_snapshots AS owned" in query
    assert "owned.review_run_id = review_runs.id" in query


def test_one_failing_table_does_not_disable_retention_for_the_others():
    # A single transaction around all nine purges means one broken statement
    # rolls the whole pass back, and the worker can only log it — so retention
    # stops for every table until someone notices. Each table gets its own
    # transaction instead.
    connection = _Connection(rowcount=4, failing_table="review_feedback_events")

    removed = purge_expired_records(connection)

    assert set(removed) == {table for table, _purge in RETENTION_PURGES}
    assert removed["review_feedback_events"] == 0
    assert all(
        count == 4 for table, count in removed.items()
        if table != "review_feedback_events"
    )
    assert connection.rollbacks == 1
    assert connection.commits == len(RETENTION_PURGES) - 1


def test_an_unrecognized_retention_flag_is_rejected_rather_than_obeyed(monkeypatch):
    # Reading anything that is not a documented "off" spelling as consent means
    # a typo in the one flag guarding nine DELETE statements silently drains the
    # database. It has to fail closed, like the API documentation flag.
    connection = _Connection()
    for unrecognized in ("maybe", "disabled", "enabled", "-1", "off ish"):
        monkeypatch.setenv("DIFFUSE_RETENTION_ENABLED", unrecognized)

        with pytest.raises(ValueError, match="DIFFUSE_RETENTION_ENABLED"):
            retention_enabled()
        with pytest.raises(ValueError, match="DIFFUSE_RETENTION_ENABLED"):
            purge_expired_records(connection)

        assert connection.cursor_instance.statements == []

    for enabled in ("1", "true", "yes", "on", "TRUE", " On "):
        monkeypatch.setenv("DIFFUSE_RETENTION_ENABLED", enabled)
        assert retention_enabled() is True

    for disabled in ("0", "false", "no", "off", "OFF", " No "):
        monkeypatch.setenv("DIFFUSE_RETENTION_ENABLED", disabled)
        assert retention_enabled() is False


def test_retention_can_be_disabled_without_touching_any_table(monkeypatch):
    connection = _Connection()
    monkeypatch.setenv("DIFFUSE_RETENTION_ENABLED", "false")

    assert not retention_enabled()
    assert purge_expired_records(connection) == {}
    assert connection.cursor_instance.statements == []


def test_a_pass_reports_every_table_it_drained():
    connection = _Connection(rowcount=3)

    removed = purge_expired_records(connection)

    assert set(removed) == {table for table, _purge in RETENTION_PURGES}
    assert set(removed.values()) == {3}
