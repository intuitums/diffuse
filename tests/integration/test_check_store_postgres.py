"""PostgreSQL coverage for `service/storage/check.py`.

Recovered from `tests/integration/test_workflow_postgres.py`, which was named
for `service/hosted/workflow.py` but held the only durability assertions for the check
run state machine. The queue assertions are dropped; the check assertions are
kept verbatim.
"""

import os
from contextlib import closing

import psycopg2

from service.hosted.workflow import claim_workflow_job, enqueue_review_event
from service.models.review import ReviewReport
from service.repositories import register_repository
from service.scm import PullRequestEvent
from service.storage.check import (
    begin_check_run,
    get_check_run_for_workflow_job,
    mark_check_run_completed,
    mark_check_run_completing,
    mark_check_run_failed,
    mark_check_run_started,
)
from service.storage.review import begin_review_run, persist_review_report


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the store schema still requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def test_check_run_creation_resumes_after_every_failure_and_completes_once():
    """A check run survives a crash at each of its four states.

    `begin_check_run` is the only handle the publisher has, so it must be safe
    to call again after a failure at creation, after the external id is known,
    and after completion -- returning the same row each time rather than
    creating a second check on the pull request.
    """
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "store/check-run",
            "number": 23,
            "web_url": "https://github.com/store/check-run/pull/23",
            "action": "opened",
            "head_sha": "8" * 40,
            "base_sha": "7" * 40,
            "updated_at": "2026-07-23T17:00:00Z",
            "delivery_id": "check-run-delivery",
        }
    )
    report = ReviewReport(
        summary="Check run fixture.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )
    external_url = "https://github.com/store/check-run/runs/github-check-456"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
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
            payload_sha256="b" * 64,
            worker_id="check-run-worker",
        )
        run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            prompt_version="check-run-v1",
            context_fingerprint="c" * 64,
        )
        persist_review_report(connection, run.id, report)

        first_check = begin_check_run(
            connection,
            review_run_id=run.id,
            scm_provider="github",
            head_sha=event.head_sha,
        )
        assert first_check.status == "creating"
        assert first_check.external_id is None
        mark_check_run_failed(connection, first_check.id)

        recovered_check = begin_check_run(
            connection,
            review_run_id=run.id,
            scm_provider="github",
            head_sha=event.head_sha,
        )
        assert recovered_check.id == first_check.id
        mark_check_run_started(
            connection,
            recovered_check.id,
            external_id="github-check-456",
            external_url=external_url,
        )
        mark_check_run_failed(connection, recovered_check.id)

        resumed_check = begin_check_run(
            connection,
            review_run_id=run.id,
            scm_provider="github",
            head_sha=event.head_sha,
        )
        assert resumed_check.external_id == "github-check-456"
        mark_check_run_started(
            connection,
            resumed_check.id,
            external_id="github-check-456",
            external_url=external_url,
        )
        mark_check_run_completing(connection, resumed_check.id)
        mark_check_run_completed(
            connection,
            resumed_check.id,
            conclusion="failure",
        )
        completed_check = begin_check_run(
            connection,
            review_run_id=run.id,
            scm_provider="github",
            head_sha=event.head_sha,
        )
        assert completed_check.status == "completed"
        assert completed_check.conclusion == "failure"
        assert get_check_run_for_workflow_job(connection, job.id) == completed_check

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, conclusion, attempt_count, external_id
                FROM review_check_runs
                WHERE review_run_id = %s
                """,
                (run.id,),
            )
            check_state = cursor.fetchone()
        connection.rollback()

    assert check_state == ("completed", "failure", 3, "github-check-456")
