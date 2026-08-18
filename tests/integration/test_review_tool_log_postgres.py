"""PostgreSQL coverage for `diffuse.review.tool_log`.

The unit suite pins the truncation arithmetic against no database at all. What
only real PostgreSQL can show is that a run's investigation survives the round
trip in the order it happened, that a retried run keeps the attempt it failed on
alongside the one it finished on, that an oversized result lands inside the CHECK
constraints rather than being rejected by them, and that an append the database
refuses leaves the caller's transaction usable -- the property that decides
whether this log can ever cost a review its findings.

`begin_review_run` is still keyed by `workflow_job_id`, so `_claimed_job` is the
same unavoidable scaffolding it is in `test_review_store_postgres.py`.
"""

import hashlib
import json
import os
from contextlib import closing
from datetime import UTC, datetime

import psycopg2
import pytest
from diffuse.database.review import begin_review_run
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.tool_log import (
    MAX_RESULT_BYTES,
    TRUNCATION_ENVELOPE_KEY,
    ReviewToolLogError,
    load_review_tool_calls,
    record_review_tool_call,
)
from diffuse.review.workflow import claim_workflow_job, enqueue_review_event


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the review-run schema requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def _review_run(connection, *, number: int, delivery_id: str, fingerprint: str):
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": f"store/tool-log-{number}",
            "number": number,
            "web_url": f"https://github.com/store/tool-log-{number}/pull/{number}",
            "action": "opened",
            "head_sha": "9" * 40,
            "base_sha": "8" * 40,
            "updated_at": "2026-07-31T09:00:00Z",
            "delivery_id": delivery_id,
        }
    )
    repository = register_repository(
        connection,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name=event.repo_full_name,
        default_branch="main",
    )
    job = _claimed_job(
        connection,
        event,
        payload_sha256=fingerprint,
        worker_id=f"tool-log-worker-{number}",
    )
    return begin_review_run(
        connection,
        workflow_job_id=job.id,
        repository_id=repository.id,
        pull_request_id=job.pull_request_id,
        index_snapshot_id=None,
        base_sha=event.base_sha,
        head_sha=event.head_sha,
        model="openai/test-review-model",
        prompt_version="native-review-v1",
        context_fingerprint=fingerprint,
    )


def _retry_the_run(connection, review_run_id):
    """The in-place reset `begin_review_run` performs when a run is retried.

    Reproduced rather than called because reaching it needs a second claim of the
    same `workflow_jobs` row, and the columns that matter to this table are these
    three. `clock_timestamp()` stands in for the production `now()`: `now()` is
    frozen for the length of a transaction, and this test keeps everything in one
    so it can roll back, so calling it here would hand the retry the attempt key
    of the attempt it replaces. Wall-clock advance between two transactions is
    what production has and what the column depends on.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'generating',
                failure_code = NULL,
                started_at = clock_timestamp(),
                updated_at = now()
            WHERE id = %s
            RETURNING started_at
            """,
            (review_run_id,),
        )
        return cursor.fetchone()[0]


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_an_investigation_round_trips_in_the_order_the_reviewer_made_it():
    """Three calls, one of them oversized and one of them failed, read back as one story.

    Ordinals are allocated by the database, so this is also the only place the
    allocation is exercised against a real UNIQUE constraint.
    """
    oversized_result = {
        "schemaVersion": "diffuse-code-search-v1",
        "sources": [
            {"filePath": f"service/{index}.py", "content": "x" * 4000}
            for index in range(30)
        ],
    }
    small_result = {
        "schemaVersion": "diffuse-code-answer-v1",
        "status": "grounded",
        "answer": "The trust boundary is validated in service/api.py.",
    }
    started = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        run = _review_run(
            connection,
            number=61,
            delivery_id="tool-log-sequence",
            fingerprint="a" * 64,
        )

        first = record_review_tool_call(
            connection,
            run.id,
            tool_name="search_code",
            arguments={"query": "trust boundary", "limit": 20},
            result=oversized_result,
            duration_ms=812,
            index_snapshot_ids=(4242, 4243),
            context_plan_fingerprint="b" * 64,
            started_at=started,
        )
        second = record_review_tool_call(
            connection,
            run.id,
            tool_name="ask_codebase",
            arguments={"question": "who validates the redirect target?"},
            duration_ms=61_000,
            failure_code="code_query_timeout",
            failure_detail="ask_codebase exceeded CODE_QUERY_MODEL_TIMEOUT_SECONDS",
        )
        third = record_review_tool_call(
            connection,
            run.id,
            tool_name="ask_codebase",
            arguments={"question": "where is the trust boundary validated?"},
            result=small_result,
            duration_ms=1_204,
            index_snapshot_ids=(4242,),
        )

        calls = load_review_tool_calls(connection, run.id)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    ordinal,
                    octet_length(result::TEXT),
                    started_at < recorded_at
                FROM review_tool_calls
                WHERE review_run_id = %s
                ORDER BY ordinal
                """,
                (run.id,),
            )
            storage = cursor.fetchall()
        connection.rollback()

    assert (first.ordinal, second.ordinal, third.ordinal) == (1, 2, 3)
    assert first.is_partial and not third.is_partial
    assert [call.ordinal for call in calls] == [1, 2, 3]
    # A run that was never retried has exactly one attempt, and it is the one
    # that produced the review.
    assert {(call.attempt, call.is_final_attempt) for call in calls} == {(1, True)}
    assert [call.tool_name for call in calls] == [
        "search_code",
        "ask_codebase",
        "ask_codebase",
    ]
    assert [call.status for call in calls] == ["succeeded", "failed", "succeeded"]

    truncated, failed, answered = calls
    # The stored copy admits what it is missing and still identifies the reply
    # the model actually received.
    assert truncated.result_truncated
    assert truncated.result_bytes == len(_canonical(oversized_result).encode())
    assert truncated.result_sha256 == hashlib.sha256(
        _canonical(oversized_result).encode()
    ).hexdigest()
    envelope = truncated.result[TRUNCATION_ENVELOPE_KEY]
    assert _canonical(oversized_result).startswith(envelope["retained_prefix"])
    assert truncated.index_snapshot_ids == (4242, 4243)
    assert truncated.context_plan_fingerprint == "b" * 64
    assert truncated.started_at == started
    assert truncated.duration_ms == 812

    # A call that answered nothing is still a step of the investigation.
    assert failed.result is None
    assert failed.result_sha256 is None
    assert failed.failure_code == "code_query_timeout"
    assert failed.index_snapshot_ids == ()

    assert answered.result == small_result
    assert not answered.result_truncated
    assert answered.succeeded

    assert [row[0] for row in storage] == [1, 2, 3]
    assert storage[0][1] <= 2 * MAX_RESULT_BYTES
    assert storage[1][1] is None
    # `started_at` was omitted for the second and third calls, so it is derived
    # from the database clock and the measured duration rather than the worker's.
    assert [row[2] for row in storage] == [True, True, True]


def test_a_retried_run_keeps_both_investigations_and_tells_them_apart():
    """A review run is reused across retries, so one run can hold several attempts.

    `begin_review_run` resets a failed run in place: same id, status back to
    `generating`, `started_at` refreshed. Keyed on the run alone this table cannot
    survive that -- the retry's first call is rejected as a duplicate of the first
    attempt's step 1, and whatever did get written reads as one impossible
    investigation with two step 1s. The sibling tables answer that by deleting and
    re-inserting, which here would throw away the record of what the reviewer was
    looking at when it failed: the most diagnostic rows in the table.
    """
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        run = _review_run(
            connection,
            number=63,
            delivery_id="tool-log-retry",
            fingerprint="d" * 64,
        )

        first_attempt = [
            record_review_tool_call(
                connection,
                run.id,
                tool_name="search_code",
                arguments={"query": "who validates the redirect target"},
                result={"resultCount": 3},
                duration_ms=140,
            ),
            record_review_tool_call(
                connection,
                run.id,
                tool_name="ask_codebase",
                arguments={"question": "is the redirect allowlist enforced?"},
                duration_ms=61_000,
                failure_code="code_query_timeout",
                failure_detail="the attempt died here",
            ),
        ]

        retried_at = _retry_the_run(connection, run.id)

        second_attempt = [
            record_review_tool_call(
                connection,
                run.id,
                tool_name="search_code",
                arguments={"query": "redirect allowlist"},
                result={"resultCount": 2},
                duration_ms=95,
            ),
            record_review_tool_call(
                connection,
                run.id,
                tool_name="ask_codebase",
                arguments={"question": "where is the allowlist compared?"},
                result={"answer": "service/api.py"},
                duration_ms=880,
            ),
        ]
        # A worker whose lease went stale during the reset is still issuing calls
        # for the attempt it began. Naming that attempt keeps its late call out of
        # the attempt that superseded it, where it would read as a step of an
        # investigation that never made it.
        straggler = record_review_tool_call(
            connection,
            run.id,
            tool_name="search_code",
            arguments={"query": "issued after the reset, belongs to attempt one"},
            result={"resultCount": 0},
            duration_ms=12,
            attempt_started_at=first_attempt[0].attempt_started_at,
        )

        calls = load_review_tool_calls(connection, run.id)
        only_first = load_review_tool_calls(connection, run.id, attempt=1)
        connection.rollback()

    # The retry restarts the step numbering instead of colliding with, or
    # continuing, the numbering of the attempt it replaced.
    assert [handle.ordinal for handle in first_attempt] == [1, 2]
    assert [handle.ordinal for handle in second_attempt] == [1, 2]
    assert straggler.ordinal == 3
    assert first_attempt[0].attempt_started_at < retried_at
    assert [handle.attempt_started_at for handle in second_attempt] == [
        retried_at,
        retried_at,
    ]

    # Nothing the failed attempt did was dropped to make room for the retry.
    assert len(calls) == 5
    assert [call.attempt for call in calls] == [1, 1, 1, 2, 2]
    assert [call.ordinal for call in calls] == [1, 2, 3, 1, 2]
    assert [call.arguments["query"] for call in calls if call.tool_name == "search_code"] == [
        "who validates the redirect target",
        "issued after the reset, belongs to attempt one",
        "redirect allowlist",
    ]
    assert [call.failure_code for call in calls] == [
        None,
        "code_query_timeout",
        None,
        None,
        None,
    ]

    # Which calls produced the review that shipped is the run's current
    # `started_at`, not the newest rows in the log -- the straggler is newer than
    # both of them and belongs to neither.
    assert [call.is_final_attempt for call in calls] == [False, False, False, True, True]
    assert {call.attempt_started_at for call in calls if call.is_final_attempt} == {retried_at}

    assert [call.ordinal for call in only_first] == [1, 2, 3]
    assert not any(call.is_final_attempt for call in only_first)


def test_a_rejected_append_leaves_the_callers_transaction_usable():
    """The log must never be the reason a review run loses everything it has written.

    Without the savepoint the failed INSERT below aborts the transaction, and the
    legitimate append that follows -- and every statement the review engine would
    have issued after it, including the one that persists the report -- fails with
    `current transaction is aborted`.
    """
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        run = _review_run(
            connection,
            number=62,
            delivery_id="tool-log-savepoint",
            fingerprint="c" * 64,
        )

        with pytest.raises(ReviewToolLogError):
            record_review_tool_call(
                connection,
                2**40,
                tool_name="search_code",
                arguments={"query": "orphaned run"},
                result={"resultCount": 0},
                duration_ms=5,
            )

        survivor = record_review_tool_call(
            connection,
            run.id,
            tool_name="search_code",
            arguments={"query": "the run that still exists"},
            result={"resultCount": 1},
            duration_ms=7,
        )
        calls = load_review_tool_calls(connection, run.id)
        connection.rollback()

    assert survivor.ordinal == 1
    assert [call.arguments["query"] for call in calls] == ["the run that still exists"]
