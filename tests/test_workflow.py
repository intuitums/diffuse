"""Queue policy that is decided in Python rather than in SQL.

The SQL itself is exercised against a real database in
`tests/integration/test_durable_work_postgres.py`; what is worth pinning here is
the classification those queries depend on, because it is the part a future
change is most likely to get wrong quietly.
"""

import pytest

from service.workflow import (
    TERMINAL_JOB_STATUSES,
    _retire_terminal_job,
    claim_stranded_review_jobs,
)


class _RecordingCursor:
    def __init__(self):
        self.statements = []

    def execute(self, query, parameters=None):
        self.statements.append((query, parameters))


@pytest.mark.parametrize("status", ["dead", "failed"])
def test_a_failed_job_releases_its_revision(status):
    """`idempotency_key` is globally unique and never expires.

    Leaving it on a job that will never run again means the revision can never
    be enqueued a second time -- so a repository whose index job died keeps
    serving a stale index with no way to ask for a new one.
    """
    cursor = _RecordingCursor()

    assert _retire_terminal_job(cursor, (7, status))

    query, parameters = cursor.statements[0]
    assert "idempotency_key = idempotency_key || '#retired-' || id" in query
    assert parameters == (7,)


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "succeeded", "cancelled", "superseded"],
)
def test_a_job_that_is_not_a_failure_keeps_its_revision(status):
    """`cancelled` and `superseded` are decisions, not failures.

    Re-enqueueing them would undo a closed pull request or resurrect a revision
    that a newer push already replaced.
    """
    cursor = _RecordingCursor()

    assert not _retire_terminal_job(cursor, (7, status))
    assert cursor.statements == []


def test_no_existing_job_is_nothing_to_retire():
    cursor = _RecordingCursor()

    assert not _retire_terminal_job(cursor, None)
    assert cursor.statements == []


def test_terminal_statuses_are_exactly_the_failure_statuses():
    assert set(TERMINAL_JOB_STATUSES) == {"dead", "failed"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"grace_seconds": -1}, "grace_seconds cannot be negative"),
        ({"limit": 0}, "between 1 and 100"),
        ({"limit": 101}, "between 1 and 100"),
    ],
)
def test_stranded_review_sweep_rejects_an_unbounded_batch(kwargs, message):
    with pytest.raises(ValueError, match=message):
        claim_stranded_review_jobs(None, **kwargs)
