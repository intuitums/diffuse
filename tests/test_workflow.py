from datetime import UTC, datetime, timedelta

import pytest

from service.scm import MIN_RATE_LIMIT_DELAY_SECONDS
from service.workflow import MAX_RATE_LIMIT_DEFERRAL_SECONDS, fail_workflow_job


class _Cursor:
    def __init__(self, attempt_count: int, max_attempts: int, young: bool):
        self.statements: list[tuple[str, tuple]] = []
        self._row = (attempt_count, max_attempts, young)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, parameters=()):
        self.statements.append((query, tuple(parameters)))

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(
        self,
        attempt_count: int = 5,
        max_attempts: int = 5,
        *,
        young: bool = True,
    ):
        self.cursor_instance = _Cursor(attempt_count, max_attempts, young)

    def cursor(self, **_kwargs):
        return self.cursor_instance


def _job_update(connection: _Connection) -> tuple[str, tuple]:
    return next(
        statement
        for statement in connection.cursor_instance.statements
        if "UPDATE workflow_jobs" in statement[0]
    )


def test_rate_limited_failure_parks_past_the_reset_without_spending_an_attempt():
    """A quota window outlives the retry curve, so the attempt must be refunded."""
    connection = _Connection(attempt_count=5, max_attempts=5)
    retry_at = datetime.now(tz=UTC) + timedelta(minutes=55)

    next_status = fail_workflow_job(
        connection,
        11,
        "worker-1",
        "provider_rate_limited",
        retry_at=retry_at,
    )

    # Without the refund this attempt is the fifth of five and the job dies.
    assert next_status == "queued"
    query, parameters = _job_update(connection)
    assert "greatest(0, attempt_count - 1)" in query
    assert "greatest(now(), %s::timestamptz)" in query
    assert parameters == (
        "queued",
        True,
        True,
        retry_at,
        True,
        480,
        "provider_rate_limited",
        True,
        11,
    )
    assert any(
        "DELETE FROM workflow_attempts" in statement[0]
        for statement in connection.cursor_instance.statements
    )


def test_a_reset_instant_in_the_past_still_parks_the_job_for_the_floor():
    """`Retry-After: 0` or a skewed clock must not make the job claimable now.

    `greatest(now(), retry_at)` resolves to now for any reset that has already
    passed, so the job is re-queued available immediately, the attempt is
    refunded, and run_once returns True — an unbounded full-speed retry loop
    against a provider that is already throttling us, holding its scope key
    against every other job that shares it.
    """
    connection = _Connection(attempt_count=1, max_attempts=5)
    before = datetime.now(tz=UTC)
    retry_at = before - timedelta(hours=1)

    fail_workflow_job(
        connection,
        11,
        "worker-1",
        "provider_rate_limited",
        retry_at=retry_at,
    )

    _, parameters = _job_update(connection)
    parked_until = parameters[3]
    assert MIN_RATE_LIMIT_DELAY_SECONDS >= 5
    assert parked_until >= before + timedelta(seconds=MIN_RATE_LIMIT_DELAY_SECONDS)


def test_quota_deferral_stops_refunding_once_the_job_is_too_old():
    """Refunds are bounded in wall-clock time so the job cannot live forever."""
    connection = _Connection(attempt_count=5, max_attempts=5, young=False)
    retry_at = datetime.now(tz=UTC) + timedelta(minutes=55)

    next_status = fail_workflow_job(
        connection,
        11,
        "worker-1",
        "provider_rate_limited",
        retry_at=retry_at,
    )

    # The provider has been shut for longer than Diffuse will wait, so the
    # attempt is finally spent and the job reaches a terminal status.
    assert next_status == "dead"
    _, parameters = _job_update(connection)
    assert parameters[1] is False
    assert any(
        "UPDATE workflow_attempts" in statement[0]
        for statement in connection.cursor_instance.statements
    )
    select_query, select_parameters = connection.cursor_instance.statements[0]
    assert "created_at > now() - (%s * interval '1 second')" in select_query
    assert select_parameters[0] == MAX_RATE_LIMIT_DEFERRAL_SECONDS


def test_ordinary_failure_still_follows_the_fixed_retry_curve():
    connection = _Connection(attempt_count=2, max_attempts=5)

    next_status = fail_workflow_job(
        connection,
        11,
        "worker-1",
        "workflow_processing_failed",
    )

    assert next_status == "queued"
    _, parameters = _job_update(connection)
    assert parameters == (
        "queued",
        False,
        False,
        None,
        True,
        60,
        "workflow_processing_failed",
        True,
        11,
    )
    assert any(
        "UPDATE workflow_attempts" in statement[0]
        for statement in connection.cursor_instance.statements
    )


def test_naive_reset_instant_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        fail_workflow_job(
            _Connection(),
            11,
            "worker-1",
            "provider_rate_limited",
            retry_at=datetime(2026, 7, 25, 12, 0, 0),
        )
