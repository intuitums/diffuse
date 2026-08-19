"""PostgreSQL coverage for `diffuse.database.conversation`.

Recovered from `tests/integration/test_workflow_postgres.py`. Nothing in the
surviving suite drives `begin_conversation_generation`,
`begin_conversation_publication` or the ignore path against real SQL.
"""

import os
from contextlib import closing

import psycopg2
from diffuse.database.conversation import (
    PublishedConversationReply,
    begin_conversation_generation,
    begin_conversation_publication,
    mark_conversation_failed,
    mark_conversation_ignored,
    mark_conversation_published,
    mark_conversation_ready,
)
from diffuse.database.finding import PublishedFindingComment, record_finding_threads
from diffuse.database.review_store import (
    begin_publication,
    begin_review_run,
    mark_publication_published,
    persist_review_report,
)
from diffuse.github.conversation_models import ConversationReference
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import PullRequestEvent, ReviewConversationEvent
from diffuse.review.workflow import (
    claim_workflow_job,
    complete_workflow_job,
    enqueue_review_conversation_event,
    enqueue_review_event,
)
from diffuse_protocol.review import (
    Category,
    ReviewFinding,
    ReviewReport,
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


def test_review_conversations_are_durable_ordered_and_retryable():
    """A conversation answer survives a failed reply, and the next turn sees it.

    Losing the generated answer on a transient reply failure means paying for it
    twice and answering differently the second time; losing the ordering means
    the follow-up question is answered without its own thread history.
    """
    repo = "store/review-conversation"
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
            "delivery_id": "conversation-review",
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

    def conversation(
        *,
        comment_id: str,
        delivery_id: str,
        question: str,
        root_comment_id: str = "701",
    ) -> ReviewConversationEvent:
        return ReviewConversationEvent(
            provider="github",
            scm_base_url="https://github.com",
            api_base_url="https://api.github.com",
            repo_full_name=repo,
            number=52,
            delivery_id=delivery_id,
            external_comment_id=comment_id,
            root_comment_id=root_comment_id,
            head_sha=review_event.head_sha,
            base_sha=review_event.base_sha,
            comment_commit_sha=review_event.head_sha,
            author="reviewer",
            author_association="MEMBER",
            created_at="2026-07-23T20:01:00Z",
            question=question,
            file_path=finding.file_path,
            line=finding.line,
            side=finding.side,
            diff_hunk="@@ -32,1 +32,2 @@\n+return account",
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
            worker_id="conversation-review-worker",
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
            prompt_version="conversation-fixture-v1",
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
            external_id="review-conversation-fixture",
            external_url="https://example/review/conversation",
        )
        _finish_job(connection, review_job.id, "conversation-review-worker")

        first_event = conversation(
            comment_id="801",
            delivery_id="conversation-question-1",
            question="Why is this exploitable across tenants?",
        )
        second_event = conversation(
            comment_id="802",
            delivery_id="conversation-question-2",
            question="Which existing helper should enforce the boundary?",
        )
        first = enqueue_review_conversation_event(
            connection,
            first_event,
            payload_sha256="c" * 64,
        )
        second = enqueue_review_conversation_event(
            connection,
            second_event,
            payload_sha256="d" * 64,
        )
        assert first.accepted and second.accepted

        first_job = claim_workflow_job(
            connection,
            "conversation-worker-1",
            lease_seconds=60,
        )
        assert first_job is not None and first_job.id == first.job_id

        first_work = begin_conversation_generation(
            connection,
            workflow_job_id=first_job.id,
        )
        assert first_work.status == "generating"
        assert first_work.finding.fingerprint == finding.fingerprint
        assert first_work.previous_turns == ()
        references = (
            ConversationReference(
                file_path=finding.file_path,
                start_line=finding.line,
                end_line=finding.line,
                explanation="The changed lookup has no tenant predicate.",
            ),
        )
        mark_conversation_ready(
            connection,
            first_work.id,
            answer="An attacker-controlled account ID can select another tenant's row.",
            references=references,
            index_snapshot_id=None,
            model="openai/test-conversation-model",
            prompt_version="conversation-v1",
            context_chunk_count=2,
            prompt_tokens=20,
            completion_tokens=6,
        )
        first_publication = begin_conversation_publication(
            connection,
            first_work.id,
        )
        assert first_publication.status == "publishing"
        mark_conversation_failed(
            connection,
            workflow_job_id=first_job.id,
            error_code="reply_create_window",
        )
        recovered_publication = begin_conversation_publication(
            connection,
            first_work.id,
        )
        assert recovered_publication.answer == first_publication.answer
        mark_conversation_published(
            connection,
            first_work.id,
            result=PublishedConversationReply(
                external_id="901",
                external_url="https://example/comment/901",
            ),
        )
        _finish_job(connection, first_job.id, "conversation-worker-1")

        second_job = claim_workflow_job(
            connection,
            "conversation-worker-2",
            lease_seconds=60,
        )
        assert second_job is not None and second_job.id == second.job_id
        second_work = begin_conversation_generation(
            connection,
            workflow_job_id=second_job.id,
        )
        assert [turn.question for turn in second_work.previous_turns] == [
            first_event.question
        ]
        assert [turn.answer for turn in second_work.previous_turns] == [
            first_publication.answer
        ]
        mark_conversation_ignored(
            connection,
            second_work.id,
            reason_code="conversation_disabled",
        )
        _finish_job(connection, second_job.id, "conversation-worker-2")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    status,
                    publication_attempt_count,
                    external_reply_id,
                    error_code
                FROM review_conversation_messages
                ORDER BY id
                """
            )
            message_states = cursor.fetchall()
        connection.rollback()

    assert message_states == [
        ("published", 2, "901", None),
        ("ignored", 0, None, "conversation_disabled"),
    ]
