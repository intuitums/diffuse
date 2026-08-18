"""PostgreSQL coverage for `diffuse.database.feedback`.

Recovered from `tests/integration/test_workflow_postgres.py`. Nothing in the
surviving suite exercises `record_review_comment_feedback`,
`reconcile_review_reactions` or `load_repository_feedback_summary` against real
SQL, and W4.1 (`diffuse learn --sync-feedback`) is a port of exactly this path.

`begin_feedback_sync` is still keyed by `workflow_job_id`, so `_claimed_job` and
`_finish_job` are unavoidable scaffolding today; nothing here asserts on queue
behaviour.
"""

import os
from contextlib import closing

import psycopg2
from diffuse.database.feedback import (
    begin_feedback_sync,
    load_repository_feedback_summary,
    reconcile_review_reactions,
    record_review_comment_feedback,
)
from diffuse.database.finding import PublishedFindingComment, record_finding_threads
from diffuse.database.review import (
    begin_publication,
    begin_review_run,
    mark_publication_published,
    persist_review_report,
)
from diffuse.github.feedback_models import ReviewReaction
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import (
    FeedbackSyncEvent,
    PullRequestEvent,
    ReviewFeedbackCommentEvent,
)
from diffuse.review.workflow import (
    claim_workflow_job,
    complete_workflow_job,
    enqueue_review_event,
    schedule_due_feedback_sync_jobs,
)
from diffuse_protocol.review import (
    Category,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
    Severity,
)


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the store schema still requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def _finish_job(connection, job_id: int, worker_id: str) -> None:
    """Scaffolding only -- releases the job so the next event can be claimed."""
    assert complete_workflow_job(connection, job_id, worker_id)


def test_review_reactions_reconcile_into_a_durable_repository_feedback_summary():
    """Authorized reactions accumulate, withdrawals subtract, and both persist.

    The summary drives rule learning, so a reaction counted twice or a withdrawal
    that never lands changes what Diffuse learns about a repository.
    """
    repo = "store/review-feedback"
    review_event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repo,
            "number": 52,
            "web_url": f"https://github.com/{repo}/pull/52",
            "action": "opened",
            "head_sha": "7" * 40,
            "base_sha": "6" * 40,
            "updated_at": "2026-07-23T20:00:00Z",
            "delivery_id": "feedback-review",
        }
    )
    finding = ReviewFinding(
        fingerprint="d" * 64,
        title="Scope the lookup to the authenticated tenant",
        body="The changed lookup accepts an arbitrary account identifier.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.94,
        file_path="service/accounts.py",
        line=33,
        side="RIGHT",
        evidence="No tenant predicate is passed to the account lookup.",
        suggested_fix="Add the authenticated tenant ID to the lookup predicate.",
    )
    report = ReviewReport(
        summary="One tenant-boundary issue.",
        risk_score=7,
        findings=[finding],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )
        review_job = _claimed_job(
            connection,
            review_event,
            payload_sha256="a" * 64,
            worker_id="feedback-review-worker",
        )
        review_run = begin_review_run(
            connection,
            workflow_job_id=review_job.id,
            repository_id=repository.id,
            pull_request_id=review_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=review_event.base_sha,
            head_sha=review_event.head_sha,
            model="openai/test-review-model",
            prompt_version="feedback-fixture-v1",
            context_fingerprint="b" * 64,
        )
        persist_review_report(connection, review_run.id, report)
        record_finding_threads(
            connection,
            review_run_id=review_run.id,
            scm_provider="github",
            comments=(
                PublishedFindingComment(
                    fingerprint=finding.fingerprint,
                    external_id="701",
                    external_node_id="PRRC_701",
                    external_url="https://example/comment/701",
                ),
            ),
        )
        publication = begin_publication(
            connection,
            review_run.id,
            scm_provider="github",
        )
        mark_publication_published(
            connection,
            publication.id,
            external_id="review-feedback-fixture",
            external_url="https://example/review/feedback",
        )
        _finish_job(connection, review_job.id, "feedback-review-worker")

        feedback_comment = ReviewFeedbackCommentEvent(
            provider="github",
            scm_base_url="https://github.com",
            api_base_url="https://api.github.com",
            repo_full_name=repo,
            number=52,
            delivery_id="feedback-comment-1",
            external_comment_id="750",
            root_comment_id="701",
            author="reviewer",
            author_association="MEMBER",
            created_at="2026-07-23T20:00:30Z",
            body="This should use our shared tenant guard.",
            file_path=finding.file_path,
        )
        assert (
            record_review_comment_feedback(
                connection,
                feedback_comment,
                payload_sha256="1" * 64,
            )
            == "recorded"
        )
        assert (
            record_review_comment_feedback(
                connection,
                feedback_comment,
                payload_sha256="1" * 64,
            )
            == "duplicate"
        )
        assert (
            schedule_due_feedback_sync_jobs(
                connection,
                api_base_url="https://api.github.com",
                interval_seconds=60,
            )
            == 1
        )
        feedback_job = claim_workflow_job(
            connection,
            "feedback-worker-1",
            lease_seconds=60,
        )
        assert feedback_job is not None
        assert feedback_job.job_type == "sync_review_feedback"
        feedback_event = FeedbackSyncEvent.from_payload(feedback_job.payload)
        feedback_target = begin_feedback_sync(
            connection,
            workflow_job_id=feedback_job.id,
            event=feedback_event,
        )
        first_sync = reconcile_review_reactions(
            connection,
            feedback_target,
            (
                ReviewReaction(
                    external_id="1001",
                    actor_login="reviewer",
                    content="+1",
                    created_at="2026-07-23T20:02:00Z",
                ),
                ReviewReaction(
                    external_id="1002",
                    actor_login="maintainer",
                    content="-1",
                    created_at="2026-07-23T20:02:30Z",
                ),
            ),
        )
        assert (first_sync.observed, first_sync.withdrawn) == (2, 0)
        _finish_job(connection, feedback_job.id, "feedback-worker-1")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE review_feedback_sync_states
                SET next_sync_at = now()
                WHERE finding_thread_id = %s
                """,
                (feedback_target.finding_thread_id,),
            )
        assert (
            schedule_due_feedback_sync_jobs(
                connection,
                api_base_url="https://api.github.com",
                interval_seconds=60,
            )
            == 1
        )
        feedback_retry = claim_workflow_job(
            connection,
            "feedback-worker-2",
            lease_seconds=60,
        )
        assert feedback_retry is not None
        second_feedback_event = FeedbackSyncEvent.from_payload(feedback_retry.payload)
        second_feedback_target = begin_feedback_sync(
            connection,
            workflow_job_id=feedback_retry.id,
            event=second_feedback_event,
        )
        second_sync = reconcile_review_reactions(
            connection,
            second_feedback_target,
            (
                ReviewReaction(
                    external_id="1001",
                    actor_login="reviewer",
                    content="+1",
                    created_at="2026-07-23T20:02:00Z",
                ),
            ),
        )
        assert (second_sync.observed, second_sync.withdrawn) == (0, 1)
        _finish_job(connection, feedback_retry.id, "feedback-worker-2")

        feedback_summary = load_repository_feedback_summary(
            connection,
            repository_id=repository.id,
        )
        assert len(feedback_summary) == 1
        assert (
            feedback_summary[0].security_classification
            == SecurityClassification.VULNERABILITY
        )
        assert feedback_summary[0].positive_reactions == 1
        assert feedback_summary[0].negative_reactions == 0
        assert feedback_summary[0].context_replies == 1
        assert feedback_summary[0].suppression_protected
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT finding_security_classification
                FROM review_feedback_events
                WHERE finding_thread_id = %s
                """,
                (feedback_target.finding_thread_id,),
            )
            assert cursor.fetchall() == [("vulnerability",)]
        connection.rollback()
