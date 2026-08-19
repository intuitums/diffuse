"""Unit coverage for `diffuse/database/review.py`'s destructive guards.

These two tests were the only thing pinning the `worker_id` lease predicate on
`mark_review_superseded`'s `UPDATE review_runs`. They lived in
`tests/test_worker.py` purely because the caller did, and went out with it --
but the guard they protect is in a module the rebuild keeps, and
`diffuse/database/review.py` calls `discard_unpublished_finding_lineage` from that
path.
"""

from diffuse.database import review_store as review_store


class _ScriptedCursor:
    """Records every statement and replays a scripted result for the first fetch."""

    def __init__(self, statements, lease_row):
        self._statements = statements
        self._lease_row = lease_row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, parameters=None):
        self._statements.append((" ".join(sql.split()), parameters))

    def fetchone(self):
        return self._lease_row


class _ScriptedConnection:
    def __init__(self, lease_row):
        self.statements: list[tuple[str, object]] = []
        self._lease_row = lease_row

    def cursor(self):
        return _ScriptedCursor(self.statements, self._lease_row)


def test_supersede_is_a_no_op_once_the_lease_has_moved_on(monkeypatch):
    """A stale worker must not retire the run another worker now owns.

    `review_runs` is keyed by `workflow_job_id` and reused across retries, so a
    worker that stalled past its lease still holds the review-run id. Without the
    lease predicate it would mark that run superseded and call
    `discard_unpublished_finding_lineage`, deleting the findings the worker which
    legitimately re-claimed the job is generating right now.
    """
    discarded: list[int] = []
    monkeypatch.setattr(
        review_store,
        "discard_unpublished_finding_lineage",
        lambda _conn, run_id: discarded.append(run_id),
    )
    connection = _ScriptedConnection(lease_row=None)

    assert review_store.mark_review_superseded(connection, 42, worker_id="stale") is False
    assert discarded == [], "a worker without the lease deleted another worker's findings"
    assert not any(
        statement.startswith("UPDATE review_runs")
        for statement, _ in connection.statements
    )


def test_supersede_proceeds_for_the_worker_still_holding_the_lease(monkeypatch):
    """The guard must not break the legitimate supersession path."""
    discarded: list[int] = []
    monkeypatch.setattr(
        review_store,
        "discard_unpublished_finding_lineage",
        lambda _conn, run_id: discarded.append(run_id),
    )
    connection = _ScriptedConnection(lease_row=(1,))

    assert review_store.mark_review_superseded(connection, 42, worker_id="owner") is True
    assert discarded == [42]
    assert any(
        statement.startswith("UPDATE review_runs")
        for statement, _ in connection.statements
    )


def test_the_lease_predicate_is_actually_in_the_statement():
    """Pin the predicate itself, not only the branch it guards.

    A port that keeps the early return but drops `worker_id` from the `WHERE`
    clause would satisfy both tests above and still let a stale holder win the
    race.
    """
    connection = _ScriptedConnection(lease_row=(1,))
    review_store.mark_review_superseded(connection, 42, worker_id="owner")

    statement, parameters = next(
        (statement, parameters)
        for statement, parameters in connection.statements
        if statement.startswith("SELECT")
    )
    assert "job.leased_by = %s" in statement
    assert "owner" in parameters
