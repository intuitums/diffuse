"""Unit coverage for `service/review/tool_log.py`'s truncation and append order.

Two properties are worth pinning without a database. Truncation must never let a
tool result blow past its budget *and* must never claim a partial copy is whole,
because a replay that silently differs from the original is the failure this log
exists to prevent. The append must allocate its ordinal in the database and must
not be able to abort the transaction the review run is writing its findings into.

Which attempt a call belongs to is a property of real rows and real resets, so it
is pinned in `tests/integration/test_review_tool_log_postgres.py`; only the shape
of the statement that resolves the attempt key is checkable here.
"""

import hashlib
import json
from datetime import UTC, datetime

import psycopg2.errors
import pytest

from service.review.tool_log import (
    MAX_RESULT_BYTES,
    TRUNCATION_ENVELOPE_KEY,
    TRUNCATION_SCHEMA_VERSION,
    ReviewToolLogError,
    bound_payload,
    record_review_tool_call,
)

_ATTEMPT_STARTED_AT = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


class _ScriptedCursor:
    """Records every statement and answers the append's RETURNING clause."""

    def __init__(self, connection):
        self._connection = connection
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, parameters=None):
        statement = " ".join(sql.split())
        self._connection.statements.append((statement, parameters))
        if not statement.startswith("INSERT INTO review_tool_calls"):
            return
        if self._connection.insert_failures:
            raise self._connection.insert_failures.pop(0)
        if self._connection.insert_matches_no_run:
            # What PostgreSQL returns when the run supplying the attempt key is
            # gone: no error, no row.
            self._row = None
            return
        self._connection.inserts += 1
        self._row = (
            7000 + self._connection.inserts,
            self._connection.inserts,
            _ATTEMPT_STARTED_AT,
        )

    def fetchone(self):
        return self._row


class _ScriptedConnection:
    def __init__(self, insert_failures=(), insert_matches_no_run=False):
        self.statements: list[tuple[str, object]] = []
        self.insert_failures = list(insert_failures)
        self.insert_matches_no_run = insert_matches_no_run
        self.inserts = 0

    def cursor(self, cursor_factory=None):
        return _ScriptedCursor(self)


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _record(connection, **overrides):
    arguments = {"query": "where is the trust boundary", "limit": 8}
    call = {
        "tool_name": "search_code",
        "arguments": arguments,
        "result": {"schemaVersion": "diffuse-code-search-v1", "resultCount": 0},
        "duration_ms": 42,
    }
    call.update(overrides)
    return record_review_tool_call(connection, 91, **call)


def test_a_payload_inside_its_budget_is_stored_whole_and_digested_as_it_arrived():
    payload = {"sources": [{"content": "x" * 100}]}

    bounded = bound_payload(payload, limit=MAX_RESULT_BYTES)

    assert bounded.payload is payload
    assert not bounded.truncated
    assert bounded.byte_length == len(_canonical(payload).encode())
    assert bounded.sha256 == hashlib.sha256(_canonical(payload).encode()).hexdigest()


def test_an_oversized_result_is_truncated_into_an_envelope_that_admits_it():
    """The stored copy must fit, say how much is missing, and identify the original.

    A `search_code` reply carries up to twenty 8,000-character sources, so this
    is the ordinary large case, not a pathological one.
    """
    payload = {"sources": [{"content": "a" * 4000} for _ in range(30)]}
    original = _canonical(payload)

    bounded = bound_payload(payload, limit=MAX_RESULT_BYTES)

    assert bounded.truncated
    assert len(_canonical(bounded.payload).encode()) <= MAX_RESULT_BYTES
    envelope = bounded.payload[TRUNCATION_ENVELOPE_KEY]
    assert envelope["schema_version"] == TRUNCATION_SCHEMA_VERSION
    assert envelope["original_bytes"] == len(original.encode()) > MAX_RESULT_BYTES
    assert envelope["original_sha256"] == bounded.sha256
    # The retained text is a genuine prefix of what arrived, so it diffs directly
    # against the canonical text a replay produces.
    assert original.startswith(envelope["retained_prefix"])
    assert envelope["retained_prefix"]


def test_the_digest_identifies_the_payload_that_arrived_not_the_copy_kept():
    """Otherwise a replay could only ever compare a truncation against itself."""
    payload = {"sources": [{"content": "b" * 4000} for _ in range(30)]}

    bounded = bound_payload(payload, limit=MAX_RESULT_BYTES)

    assert bounded.sha256 != hashlib.sha256(_canonical(bounded.payload).encode()).hexdigest()
    assert bounded.byte_length > MAX_RESULT_BYTES


def test_an_escape_heavy_payload_still_fits_its_budget():
    """Re-escaping the retained prefix is what makes a one-shot slice too long.

    Every quote and backslash in the retained text costs two bytes once it goes
    back into JSON as a string, so budgeting `limit - envelope_overhead` and
    slicing once overflows by roughly the escape density of the payload -- for
    source code carrying quoted strings, that is not a rounding error.
    """
    payload = {"sources": [{"content": '"\\' * 3000} for _ in range(20)]}

    bounded = bound_payload(payload, limit=MAX_RESULT_BYTES)

    assert bounded.truncated
    assert len(_canonical(bounded.payload).encode()) <= MAX_RESULT_BYTES


def test_the_envelope_has_a_floor_and_the_shrinking_budget_terminates_at_it():
    """Below the envelope's own size nothing can be retained, and the loop must stop.

    Both real budgets are two orders of magnitude above this floor, so the case
    is a termination proof rather than a limit anyone will hit.
    """
    payload = {"content": "c" * 500}

    bounded = bound_payload(payload, limit=64)

    assert bounded.truncated
    assert bounded.payload[TRUNCATION_ENVELOPE_KEY]["retained_prefix"] == ""


def test_the_ordinal_is_allocated_by_the_database_inside_the_insert():
    """Reading the maximum first would let two calls in one transaction collide."""
    connection = _ScriptedConnection()

    first = _record(connection)
    second = _record(connection)

    statement, parameters = next(
        item
        for item in connection.statements
        if item[0].startswith("INSERT INTO review_tool_calls")
    )
    assert "COALESCE(MAX(existing.ordinal), 0) + 1" in statement
    # The maximum is taken over one attempt of the run, not over the run, or a
    # retry would keep counting from the attempt it replaced.
    assert "AND existing.attempt_started_at = attempt.started_at" in statement
    assert parameters[-1] == 91
    assert (first.ordinal, second.ordinal) == (1, 2)
    assert not first.is_partial


def test_the_attempt_key_defaults_to_the_one_the_review_run_row_carries():
    """`begin_review_run` reuses a run across retries and only refreshes `started_at`.

    Nothing else on the run distinguishes the second attempt from the first, and
    a caller that knows nothing about attempts must still not collide with the
    rows the previous attempt left behind.
    """
    connection = _ScriptedConnection()

    handle = _record(connection)
    passed = _record(connection, attempt_started_at=_ATTEMPT_STARTED_AT)

    statement, parameters = next(
        item
        for item in connection.statements
        if item[0].startswith("INSERT INTO review_tool_calls")
    )
    assert "COALESCE(%s::TIMESTAMPTZ, started_at) AS started_at" in statement
    assert "FROM review_runs WHERE id = %s" in statement
    assert parameters[-2] is None
    assert connection.statements[-2][1][-2] == _ATTEMPT_STARTED_AT
    assert handle.attempt_started_at == passed.attempt_started_at


def test_an_append_against_a_run_that_is_gone_is_reported_rather_than_assumed():
    """No run row means no attempt key, so the INSERT selects nothing and raises no error.

    Returning a handle here would tell the caller a step was logged that a replay
    will never find, which is worse than the append failing loudly.
    """
    connection = _ScriptedConnection(insert_matches_no_run=True)

    with pytest.raises(ReviewToolLogError, match="review run is gone"):
        _record(connection)

    statements = [statement for statement, _ in connection.statements]
    assert statements[-1] == "ROLLBACK TO SAVEPOINT diffuse_review_tool_call"


def test_a_failed_tool_call_is_recorded_as_data_rather_than_raised():
    """What the reviewer asked and did not get told is part of what it saw."""
    connection = _ScriptedConnection()

    handle = _record(
        connection,
        result=None,
        failure_code="code_query_timeout",
        failure_detail="ask_codebase exceeded CODE_QUERY_MODEL_TIMEOUT_SECONDS",
    )

    _, parameters = next(
        item
        for item in connection.statements
        if item[0].startswith("INSERT INTO review_tool_calls")
    )
    assert handle.ordinal == 1
    assert parameters[5] == "failed"
    assert parameters[6] is None
    assert parameters[10] == "code_query_timeout"


def test_a_call_that_neither_answered_nor_failed_is_rejected_before_any_sql():
    """A row with no result and no failure code would describe nothing at all."""
    connection = _ScriptedConnection()

    with pytest.raises(ValueError, match="result or a failure code"):
        _record(connection, result=None)

    assert connection.statements == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"tool_name": "Search Code"}, "tool name is invalid"),
        ({"failure_code": "TIMED OUT", "result": None}, "failure code is invalid"),
        ({"context_plan_fingerprint": "not-a-digest"}, "fingerprint is invalid"),
        ({"duration_ms": -1}, "duration cannot be negative"),
        ({"index_snapshot_ids": (0,)}, "positive and bounded"),
    ],
)
def test_invalid_calls_never_reach_the_database(overrides, message):
    """Validation runs before the savepoint so a bad argument cannot touch the run."""
    connection = _ScriptedConnection()

    with pytest.raises(ValueError, match=message):
        _record(connection, **overrides)

    assert connection.statements == []


def test_a_lost_ordinal_race_rolls_back_to_the_savepoint_and_appends_anyway():
    """A stale lease can put two workers on one run; losing the row is not an option."""
    connection = _ScriptedConnection(
        insert_failures=[psycopg2.errors.UniqueViolation("duplicate key")]
    )

    handle = _record(connection)

    statements = [statement for statement, _ in connection.statements]
    assert statements.count("SAVEPOINT diffuse_review_tool_call") == 2
    assert "ROLLBACK TO SAVEPOINT diffuse_review_tool_call" in statements
    assert statements[-1] == "RELEASE SAVEPOINT diffuse_review_tool_call"
    assert handle.ordinal == 1


def test_an_unrecoverable_append_releases_the_transaction_it_borrowed():
    """The log must never be the reason a review run loses its findings.

    Without the savepoint, a rejected row leaves the surrounding transaction in an
    aborted state, and every later statement the review engine issues -- including
    the one that persists the report -- fails.
    """
    connection = _ScriptedConnection(
        insert_failures=[psycopg2.errors.ForeignKeyViolation("review run is gone")]
    )

    with pytest.raises(ReviewToolLogError):
        _record(connection)

    statements = [statement for statement, _ in connection.statements]
    assert statements[-1] == "ROLLBACK TO SAVEPOINT diffuse_review_tool_call"
