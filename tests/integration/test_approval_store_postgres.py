"""PostgreSQL coverage for `service/approval_store.py`.

Recovered from `tests/integration/test_workflow_postgres.py`. This is W1.3's
done-when: an auto-approval decision and its publication must be durable and
idempotent, so a retry after a failed publish re-uses the same decision row
instead of approving the pull request twice.
"""

import os
from contextlib import closing

import psycopg2

from service.approval_store import (
    begin_auto_approval,
    mark_auto_approval_failed,
    mark_auto_approval_published,
)
from service.auto_approval import AutoApprovalDecision, AutoApprovalRisk
from service.repositories import register_repository
from service.review_models import ReviewReport
from service.review_store import (
    begin_publication,
    begin_review_run,
    mark_publication_published,
    persist_review_report,
)
from service.scm import PullRequestEvent
from service.workflow import claim_workflow_job, enqueue_review_event


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the store schema still requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def test_auto_approval_decision_and_publication_are_durable_and_idempotent():
    provider = "github"
    scm_base_url = "https://github.com"
    api_base_url = "https://api.github.com"
    review_url = f"{scm_base_url}/store/auto-approval/pull/24"
    event = PullRequestEvent.from_payload(
        {
            "provider": provider,
            "scm_base_url": scm_base_url,
            "api_base_url": api_base_url,
            "repo_full_name": "store/auto-approval",
            "number": 24,
            "web_url": review_url,
            "action": "opened",
            "head_sha": "9" * 40,
            "base_sha": "8" * 40,
            "updated_at": "2026-07-23T17:30:00Z",
            "delivery_id": "auto-approval-delivery",
        }
    )
    report = ReviewReport(
        summary="No issues.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=20,
        completion_tokens=4,
    )
    decision = AutoApprovalDecision(
        eligible=True,
        reason_code="approved",
        message="All automatic-approval checks passed.",
        risk_level=AutoApprovalRisk.LOW,
        risk_ceiling=AutoApprovalRisk.LOW,
        changed_paths=("docs/guide.md",),
        changed_file_count=1,
        changed_line_count=2,
        diff_chars=128,
    )

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider=provider,
            scm_base_url=scm_base_url,
            full_name=event.repo_full_name,
            default_branch="main",
        )
        job = _claimed_job(
            connection,
            event,
            payload_sha256="d" * 64,
            worker_id="auto-approval-worker",
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
            prompt_version="auto-approval-v1",
            context_fingerprint="c" * 64,
        )
        persist_review_report(connection, run.id, report)
        publication = begin_publication(
            connection,
            run.id,
            scm_provider=provider,
        )
        mark_publication_published(
            connection,
            publication.id,
            external_id="github-review-approval-fixture",
            external_url="https://example/review/approval-fixture",
        )

        first = begin_auto_approval(
            connection,
            review_run_id=run.id,
            scm_provider=provider,
            head_sha=event.head_sha,
            policy_fingerprint="e" * 64,
            decision=decision,
        )
        assert first.status == "publishing"
        mark_auto_approval_failed(connection, first.id)
        retry = begin_auto_approval(
            connection,
            review_run_id=run.id,
            scm_provider=provider,
            head_sha=event.head_sha,
            policy_fingerprint="e" * 64,
            decision=decision,
        )
        assert retry.id == first.id
        assert retry.status == "publishing"
        mark_auto_approval_published(
            connection,
            retry.id,
            external_id=f"{provider}-approval-901",
            external_url="https://example/review/901",
        )
        recovered = begin_auto_approval(
            connection,
            review_run_id=run.id,
            scm_provider=provider,
            head_sha=event.head_sha,
            policy_fingerprint="e" * 64,
            decision=decision,
        )
        assert recovered.status == "published"
        assert recovered.external_id == f"{provider}-approval-901"

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    eligible,
                    decision_reason,
                    risk_level,
                    risk_ceiling,
                    changed_paths,
                    status,
                    attempt_count,
                    external_id
                FROM review_auto_approvals
                WHERE review_run_id = %s
                """,
                (run.id,),
            )
            approval_state = cursor.fetchone()
        connection.rollback()

    assert approval_state == (
        True,
        "approved",
        "low",
        "low",
        ["docs/guide.md"],
        "published",
        2,
        f"{provider}-approval-901",
    )
