"""PostgreSQL coverage for stranded reclaim of failed checks with a remote id.

DEV-313: a check that failed the provider PATCH after persisting `external_id`
must still be selected by `claim_stranded_review_jobs`.
"""

from __future__ import annotations

import os
from contextlib import closing

import psycopg2

from service.hosted.workflow import (
    claim_stranded_review_jobs,
    claim_workflow_job,
    enqueue_review_event,
)
from service.models.review import ReviewReport
from service.repositories import register_repository
from service.scm import PullRequestEvent
from service.storage.check import (
    begin_check_run,
    mark_check_run_failed,
    mark_check_run_started,
)
from service.storage.review import (
    begin_review_run,
    mark_review_terminal_failed,
    persist_review_report,
)


def _event(*, number: int, delivery: str) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": f"store/stranded-check-{number}",
            "number": number,
            "web_url": f"https://github.com/store/stranded-check-{number}/pull/{number}",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-08-03T12:00:00Z",
            "delivery_id": delivery,
        }
    )


def _report() -> ReviewReport:
    return ReviewReport(
        summary="Stranded check fixture.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=1,
        completion_tokens=1,
    )


def _dead_job_with_check(
    connection,
    *,
    event: PullRequestEvent,
    payload_sha256: str,
    worker_id: str,
    persist_external_id: bool,
):
    repository = register_repository(
        connection,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name=event.repo_full_name,
        default_branch="main",
    )
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    run = begin_review_run(
        connection,
        workflow_job_id=job.id,
        repository_id=repository.id,
        pull_request_id=job.pull_request_id,
        index_snapshot_id=None,
        base_sha=event.base_sha,
        head_sha=event.head_sha,
        model="openai/test-review-model",
        prompt_version="stranded-check-v1",
        context_fingerprint="e" * 64,
    )
    persist_review_report(connection, run.id, _report())
    check = begin_check_run(
        connection,
        review_run_id=run.id,
        scm_provider="github",
        head_sha=event.head_sha,
    )
    if persist_external_id:
        mark_check_run_started(
            connection,
            check.id,
            external_id=f"github-check-{job.id}",
            external_url=f"https://example/check/{job.id}",
        )
    mark_check_run_failed(connection, check.id)

    # Simulate a lease-swept terminal job whose check PATCH already failed once.
    # Age completed_at past grace; mark the review terminal so only the check
    # EXISTS clause can keep this job claimable.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'dead',
                leased_by = NULL,
                lease_expires_at = NULL,
                completed_at = now() - interval '1 hour',
                updated_at = now()
            WHERE id = %s
            """,
            (job.id,),
        )
    mark_review_terminal_failed(connection, job.id)
    return job.id


def test_claim_stranded_includes_failed_check_with_external_id():
    event = _event(number=313, delivery="stranded-failed-with-id")
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        connection.autocommit = False
        job_id = _dead_job_with_check(
            connection,
            event=event,
            payload_sha256="1" * 64,
            worker_id="stranded-with-id",
            persist_external_id=True,
        )
        claimed = claim_stranded_review_jobs(connection, grace_seconds=0, limit=20)
        connection.rollback()

    assert any(job.id == job_id for job in claimed)


def test_claim_stranded_excludes_failed_check_without_external_id():
    """Null-id failed rows are a permanent dead end after DEV-289 recovery fails."""
    event = _event(number=314, delivery="stranded-failed-without-id")
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        connection.autocommit = False
        job_id = _dead_job_with_check(
            connection,
            event=event,
            payload_sha256="2" * 64,
            worker_id="stranded-without-id",
            persist_external_id=False,
        )
        claimed = claim_stranded_review_jobs(connection, grace_seconds=0, limit=20)
        connection.rollback()

    assert all(job.id != job_id for job in claimed)
