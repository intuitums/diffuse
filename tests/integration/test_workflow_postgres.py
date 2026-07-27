import os
from contextlib import closing

import psycopg2
import pytest

from indexer.store import begin_index_snapshot
from repository_policy.resolve import ApprovedCustomContext
from retriever.context_models import RepositoryContextSnapshot
from service.approval_store import (
    begin_auto_approval,
    mark_auto_approval_failed,
    mark_auto_approval_published,
)
from service.auto_approval import AutoApprovalDecision, AutoApprovalRisk
from service.check_store import (
    begin_check_run,
    get_check_run_for_workflow_job,
    mark_check_run_completed,
    mark_check_run_completing,
    mark_check_run_failed,
    mark_check_run_started,
)
from service.conversation_models import ConversationReference
from service.conversation_store import (
    PublishedConversationReply,
    begin_conversation_generation,
    begin_conversation_publication,
    mark_conversation_failed,
    mark_conversation_ignored,
    mark_conversation_published,
    mark_conversation_ready,
)
from service.feedback_models import ReviewReaction
from service.feedback_store import (
    begin_feedback_sync,
    load_repository_feedback_summary,
    reconcile_review_reactions,
    record_review_comment_feedback,
)
from service.finding_store import (
    PublishedFindingComment,
    PublishedThreadOperation,
    begin_thread_operations,
    latest_published_review_head,
    load_review_continuity,
    mark_thread_operation_published,
    record_finding_threads,
)
from service.learning_models import (
    RuleLearningJobEvent,
    SuggestedRuleBatch,
    SuggestedRuleCandidate,
)
from service.learning_store import (
    begin_rule_learning,
    list_learned_rules,
    load_active_learned_rules,
    load_learned_rule_audit,
    moderate_learned_rule,
    persist_rule_suggestions,
    queue_rule_learning_job,
    schedule_due_rule_learning_jobs,
)
from service.repositories import register_repository, set_repository_enabled
from service.review_models import (
    Category,
    ReviewDiagram,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
    Severity,
)
from service.review_store import (
    begin_publication,
    begin_review_run,
    load_review_report,
    mark_publication_failed,
    mark_publication_published,
    mark_review_superseded,
    persist_review_report,
)
from service.scm import (
    FeedbackSyncEvent,
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
)
from service.workflow import (
    DeliveryConflictError,
    claim_workflow_job,
    complete_workflow_job,
    enqueue_repository_index_event,
    enqueue_review_conversation_event,
    enqueue_review_event,
    fail_workflow_job,
    heartbeat_workflow_job,
    schedule_due_feedback_sync_jobs,
    supersede_workflow_job,
    workflow_job_is_current,
    workflow_job_is_latest,
)


def _event(*, delivery: str, head: str, updated_at: str) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "workflow/repo",
            "number": 19,
            "web_url": "https://github.com/workflow/repo/pull/19",
            "action": "synchronize",
            "head_sha": head,
            "base_sha": "b" * 40,
            "updated_at": updated_at,
            "delivery_id": delivery,
        }
    )


def _rich_event(
    *,
    repo: str,
    action: str,
    delivery: str,
    updated_at: str,
    draft: bool,
    labels: list[str],
) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repo,
            "number": 31,
            "web_url": f"https://github.com/{repo}/pull/31",
            "action": action,
            "head_sha": "4" * 40,
            "base_sha": "3" * 40,
            "updated_at": updated_at,
            "delivery_id": delivery,
            "author": "octocat",
            "base_branch": "main",
            "head_branch": "feature/trigger-policy",
            "is_draft": draft,
            "labels": labels,
            "title": "Add trigger policy",
            "description": "Exercises metadata-sensitive review identity.",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 2,
        }
    )


def test_delivery_idempotency_supersession_retry_and_completion():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    first_event = _event(
        delivery="delivery-1",
        head="a" * 40,
        updated_at="2026-07-23T15:30:00Z",
    )
    same_revision_event = _event(
        delivery="delivery-2",
        head="a" * 40,
        updated_at="2026-07-23T15:30:00Z",
    )
    new_event = _event(
        delivery="delivery-3",
        head="c" * 40,
        updated_at="2026-07-23T15:31:00Z",
    )
    stale_event = _event(
        delivery="delivery-stale",
        head="d" * 40,
        updated_at="2026-07-23T15:29:00Z",
    )
    lease_recovery_event = _event(
        delivery="delivery-lease-recovery",
        head="f" * 40,
        updated_at="2026-07-23T15:32:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        begin_index_snapshot(
            connection,
            "workflow/repo",
            "e" * 40,
            "integration-model",
            1536,
        )
        first = enqueue_review_event(
            connection,
            first_event,
            payload_sha256="1" * 64,
        )
        duplicate_delivery = enqueue_review_event(
            connection,
            first_event,
            payload_sha256="1" * 64,
        )
        duplicate_revision = enqueue_review_event(
            connection,
            same_revision_event,
            payload_sha256="2" * 64,
        )
        newer = enqueue_review_event(
            connection,
            new_event,
            payload_sha256="3" * 64,
        )
        stale = enqueue_review_event(
            connection,
            stale_event,
            payload_sha256="4" * 64,
        )

        assert first.accepted
        assert duplicate_delivery.job_id == first.job_id
        assert duplicate_delivery.state == "duplicate_delivery:queued"
        assert duplicate_revision.job_id == first.job_id
        assert duplicate_revision.state == "duplicate_revision:queued"
        assert newer.accepted
        assert newer.job_id != first.job_id
        assert stale.state == "stale_delivery"

        job = claim_workflow_job(connection, "integration-worker", lease_seconds=60)
        assert job is not None
        assert job.id == newer.job_id
        assert job.revision == "c" * 40
        assert workflow_job_is_current(connection, job.id, "integration-worker")
        assert heartbeat_workflow_job(
            connection,
            job.id,
            "integration-worker",
            lease_seconds=120,
        )
        assert (
            fail_workflow_job(
                connection,
                job.id,
                "integration-worker",
                "temporary_failure",
                base_delay_seconds=1,
            )
            == "queued"
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_jobs SET available_at = now() WHERE id = %s",
                (job.id,),
            )
        retry = claim_workflow_job(connection, "integration-worker", lease_seconds=60)
        assert retry is not None
        assert retry.id == job.id
        assert retry.attempt_count == 2
        assert complete_workflow_job(connection, retry.id, "integration-worker")

        lease_recovery = enqueue_review_event(
            connection,
            lease_recovery_event,
            payload_sha256="5" * 64,
        )
        abandoned = claim_workflow_job(connection, "abandoned-worker", lease_seconds=60)
        assert abandoned is not None
        assert abandoned.id == lease_recovery.job_id
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET lease_expires_at = now() - interval '1 second'
                WHERE id = %s
                """,
                (abandoned.id,),
            )
        assert not heartbeat_workflow_job(
            connection,
            abandoned.id,
            "abandoned-worker",
            lease_seconds=60,
        )
        assert not complete_workflow_job(connection, abandoned.id, "abandoned-worker")
        recovered = claim_workflow_job(connection, "replacement-worker", lease_seconds=60)
        assert recovered is not None
        assert recovered.id == abandoned.id
        assert recovered.attempt_count == 2
        assert complete_workflow_job(connection, recovered.id, "replacement-worker")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, attempt_count
                FROM workflow_jobs
                WHERE scope_key = %s
                ORDER BY id
                """,
                (new_event.scope_key,),
            )
            jobs = cursor.fetchall()
            cursor.execute(
                """
                SELECT status
                FROM workflow_attempts
                WHERE workflow_job_id = %s
                ORDER BY attempt_number
                """,
                (retry.id,),
            )
            attempts = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT status
                FROM workflow_attempts
                WHERE workflow_job_id = %s
                ORDER BY attempt_number
                """,
                (recovered.id,),
            )
            recovered_attempts = [row[0] for row in cursor.fetchall()]

        with pytest.raises(DeliveryConflictError):
            enqueue_review_event(
                connection,
                first_event,
                payload_sha256="9" * 64,
            )
        connection.rollback()

    assert jobs == [
        (first.job_id, "superseded", 0),
        (newer.job_id, "succeeded", 2),
        (lease_recovery.job_id, "succeeded", 2),
    ]
    assert attempts == ["failed", "succeeded"]
    assert recovered_attempts == ["lease_expired", "succeeded"]


def _push_event(*, delivery: str, after: str, pushed_at: str) -> PushEvent:
    return PushEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="workflow/index-repo",
        ref_name="refs/heads/main",
        default_branch="main",
        before_sha="0" * 40,
        after_sha=after,
        pushed_at=pushed_at,
        delivery_id=delivery,
    )


def test_index_jobs_serialize_by_repository_ref_and_ignore_stale_pushes():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    first_event = _push_event(
        delivery="index-delivery-1",
        after="1" * 40,
        pushed_at="2026-07-23T16:00:00Z",
    )
    newer_event = _push_event(
        delivery="index-delivery-2",
        after="2" * 40,
        pushed_at="2026-07-23T16:01:00Z",
    )
    duplicate_revision = _push_event(
        delivery="index-delivery-3",
        after="2" * 40,
        pushed_at="2026-07-23T16:01:00Z",
    )
    stale_event = _push_event(
        delivery="index-delivery-stale",
        after="3" * 40,
        pushed_at="2026-07-23T15:59:00Z",
    )
    cancelled_event = _push_event(
        delivery="index-delivery-cancelled",
        after="4" * 40,
        pushed_at="2026-07-23T16:02:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="workflow/index-repo",
            default_branch="main",
        )
        first = enqueue_repository_index_event(
            connection,
            first_event,
            payload_sha256="6" * 64,
        )
        running = claim_workflow_job(connection, "index-worker-1", lease_seconds=60)
        assert running is not None
        assert running.id == first.job_id
        assert running.pull_request_id is None
        assert running.job_type == "index_repository"

        newer = enqueue_repository_index_event(
            connection,
            newer_event,
            payload_sha256="7" * 64,
        )
        assert not workflow_job_is_latest(connection, running.id, "index-worker-1")
        assert claim_workflow_job(connection, "index-worker-2", lease_seconds=60) is None
        assert supersede_workflow_job(connection, running.id, "index-worker-1")

        replacement = claim_workflow_job(connection, "index-worker-2", lease_seconds=60)
        assert replacement is not None
        assert replacement.id == newer.job_id
        assert workflow_job_is_latest(connection, replacement.id, "index-worker-2")
        assert complete_workflow_job(connection, replacement.id, "index-worker-2")

        duplicate = enqueue_repository_index_event(
            connection,
            duplicate_revision,
            payload_sha256="8" * 64,
        )
        stale = enqueue_repository_index_event(
            connection,
            stale_event,
            payload_sha256="9" * 64,
        )
        assert duplicate.job_id == newer.job_id
        assert duplicate.state == "duplicate_revision:succeeded"
        assert stale.state == "stale_delivery"

        cancelled = enqueue_repository_index_event(
            connection,
            cancelled_event,
            payload_sha256="a" * 64,
        )
        assert set_repository_enabled(connection, repository.id, False)
        assert claim_workflow_job(connection, "index-worker-3", lease_seconds=60) is None

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status
                FROM workflow_jobs
                WHERE repository_id = %s
                ORDER BY id
                """,
                (repository.id,),
            )
            jobs = cursor.fetchall()
        connection.rollback()

    assert jobs == [
        (first.job_id, "superseded"),
        (newer.job_id, "succeeded"),
        (cancelled.job_id, "cancelled"),
    ]


def test_native_review_report_and_publication_are_durable_and_idempotent():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "workflow/native-review",
            "number": 23,
            "web_url": "https://github.com/workflow/native-review/pull/23",
            "action": "opened",
            "head_sha": "8" * 40,
            "base_sha": "7" * 40,
            "updated_at": "2026-07-23T17:00:00Z",
            "delivery_id": "native-review-delivery",
        }
    )
    finding = ReviewFinding(
        fingerprint="f" * 64,
        title="Validate the new trust boundary",
        body="The changed line accepts untrusted data without validating it.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.91,
        file_path="service/api.py",
        line=42,
        side="RIGHT",
        evidence="The new call directly forwards user-controlled input.",
        suggested_fix="Validate and normalize the value before forwarding it.",
    )
    preventative_finding = ReviewFinding(
        fingerprint="e" * 64,
        title="Constrain the future redirect target",
        body=(
            "The new helper accepts an unrestricted target that is not currently "
            "reachable from untrusted input."
        ),
        severity=Severity.MEDIUM,
        category=Category.SECURITY,
        security_classification=SecurityClassification.PREVENTATIVE,
        confidence=0.93,
        file_path="service/redirects.py",
        line=18,
        side="RIGHT",
        evidence=(
            "Only trusted constants call the helper in this snapshot, but a future "
            "untrusted caller would create an open redirect."
        ),
        suggested_fix="Accept a route identifier instead of an arbitrary target.",
    )
    report = ReviewReport(
        summary="One vulnerability and one preventative risk were found.",
        risk_score=7,
        confidence_score=2,
        diagram=ReviewDiagram(
            kind="sequence",
            title="Validated request flow",
            mermaid=(
                "sequenceDiagram\n"
                "  Client->>API: request\n"
                "  API->>Validator: validate"
            ),
        ),
        diagram_collapsible=True,
        diagram_default_open=False,
        summary_section_collapsible=True,
        summary_section_default_open=False,
        issues_table_section_collapsible=True,
        issues_table_section_default_open=False,
        confidence_score_section_collapsible=True,
        confidence_score_section_default_open=False,
        footer_included=False,
        update_description=True,
        summary_comment_enabled=False,
        fix_with_agent_enabled=False,
        findings=[finding, preventative_finding],
        diff_file_count=2,
        reviewed_file_count=2,
        context_chunk_count=3,
        prompt_tokens=120,
        completion_tokens=30,
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=event.repo_full_name,
            default_branch="main",
        )
        related_repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="workflow/native-review-shared",
            default_branch="main",
        )
        related_snapshot = begin_index_snapshot(
            connection,
            related_repository.full_name,
            "6" * 40,
            "integration-context-model",
            1536,
        )
        context_snapshots = (
            RepositoryContextSnapshot(
                repository_id=related_repository.id,
                repository_full_name=related_repository.full_name,
                snapshot_id=related_snapshot.snapshot_id,
                commit_sha="6" * 40,
                source="cluster",
                cluster_ids=(91,),
            ),
        )
        queued = enqueue_review_event(
            connection,
            event,
            payload_sha256="b" * 64,
        )
        job = claim_workflow_job(connection, "native-review-worker", lease_seconds=60)
        assert job is not None
        assert job.id == queued.job_id
        assert job.pull_request_id is not None

        run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            verifier_model="openai/test-verifier-model",
            provenance={
                "schema_version": "diffuse-review-provenance-v1",
                "classification": "ai_assisted",
                "model_family": "anthropic",
                "confidence": 0.9,
            },
            model_routing_reason="opposing_anthropic_reviewer",
            prompt_version="native-review-v1",
            context_fingerprint="c" * 64,
            context_snapshots=context_snapshots,
        )
        assert run.needs_generation
        persist_review_report(connection, run.id, report)
        assert load_review_report(connection, run.id) == report

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
            external_url=(
                "https://github.com/workflow/native-review/"
                "runs/github-check-456"
            ),
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
            external_url=(
                "https://github.com/workflow/native-review/"
                "runs/github-check-456"
            ),
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

        first_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert first_publication.status == "publishing"
        assert first_publication.review_number == 1
        mark_publication_failed(connection, first_publication.id)

        retry_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert retry_publication.id == first_publication.id
        assert retry_publication.review_number == 1
        mark_publication_published(
            connection,
            retry_publication.id,
            external_id="github-review-123",
            external_url="https://github.com/workflow/native-review/pull/23#pullrequestreview-123",
        )

        resumed_run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            prompt_version="native-review-v1",
            context_fingerprint="c" * 64,
        )
        resumed_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert resumed_run.status == "published"
        assert not resumed_run.needs_generation
        assert resumed_publication.status == "published"
        assert resumed_publication.external_id == "github-review-123"
        assert resumed_publication.review_number == 1
        assert complete_workflow_job(connection, job.id, "native-review-worker")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    run.status,
                    publication.status,
                    publication.attempt_count,
                    run.confidence_score,
                    run.review_number,
                    run.verifier_model,
                    run.provenance->>'model_family',
                    run.model_routing_reason
                FROM review_runs AS run
                JOIN review_publications AS publication
                  ON publication.review_run_id = run.id
                WHERE run.id = %s
                """,
                (run.id,),
            )
            statuses = cursor.fetchone()
            cursor.execute(
                """
                SELECT fingerprint, security_classification
                FROM review_findings
                WHERE review_run_id = %s
                ORDER BY ordinal
                """,
                (run.id,),
            )
            finding_classifications = cursor.fetchall()
            cursor.execute(
                """
                SELECT status, conclusion, attempt_count, external_id
                FROM review_check_runs
                WHERE review_run_id = %s
                """,
                (run.id,),
            )
            check_state = cursor.fetchone()
            cursor.execute(
                """
                SELECT
                    repository_id,
                    repository_full_name,
                    snapshot_id,
                    commit_sha,
                    relation_kind,
                    cluster_ids,
                    ordinal
                FROM review_run_context_snapshots
                WHERE review_run_id = %s
                """,
                (run.id,),
            )
            context_state = cursor.fetchone()
        connection.rollback()

    assert statuses == (
        "published",
        "published",
        2,
        2,
        1,
        "openai/test-verifier-model",
        "anthropic",
        "opposing_anthropic_reviewer",
    )
    assert finding_classifications == [
        (finding.fingerprint, "vulnerability"),
        (preventative_finding.fingerprint, "preventative"),
    ]
    assert check_state == ("completed", "failure", 3, "github-check-456")
    assert context_state == (
        related_repository.id,
        "workflow/native-review-shared",
        related_snapshot.snapshot_id,
        "6" * 40,
        "cluster",
        [91],
        1,
    )


def test_auto_approval_decision_and_publication_are_durable_and_idempotent():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    provider = "github"
    scm_base_url = "https://github.com"
    api_base_url = "https://api.github.com"
    review_url = f"{scm_base_url}/workflow/auto-approval/pull/24"
    event = PullRequestEvent.from_payload(
        {
            "provider": provider,
            "scm_base_url": scm_base_url,
            "api_base_url": api_base_url,
            "repo_full_name": "workflow/auto-approval",
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

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider=provider,
            scm_base_url=scm_base_url,
            full_name=event.repo_full_name,
            default_branch="main",
        )
        queued = enqueue_review_event(
            connection,
            event,
            payload_sha256="d" * 64,
        )
        job = claim_workflow_job(
            connection,
            "auto-approval-worker",
            lease_seconds=60,
        )
        assert job is not None and job.id == queued.job_id
        assert job.pull_request_id is not None
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
        assert complete_workflow_job(
            connection,
            job.id,
            "auto-approval-worker",
        )

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


def test_ready_transition_same_revision_supersedes_draft_and_skip_is_durable():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "workflow/trigger-policy"
    draft = _rich_event(
        repo=repo,
        action="opened",
        delivery="trigger-draft",
        updated_at="2026-07-23T18:00:00Z",
        draft=True,
        labels=["wip"],
    )
    ready = _rich_event(
        repo=repo,
        action="ready_for_review",
        delivery="trigger-ready",
        updated_at="2026-07-23T18:01:00Z",
        draft=False,
        labels=["needs-review"],
    )
    skipped_report = ReviewReport(
        summary="Automatic review is disabled by repository policy.",
        risk_score=0,
        findings=[],
        diff_file_count=2,
        reviewed_file_count=0,
        ignored_file_count=0,
        inline_comments_enabled=False,
        publication_enabled=False,
        skip_reason="automatic_disabled",
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )
        draft_result = enqueue_review_event(
            connection,
            draft,
            payload_sha256="d" * 64,
        )
        ready_result = enqueue_review_event(
            connection,
            ready,
            payload_sha256="e" * 64,
        )
        assert draft_result.job_id != ready_result.job_id

        job = claim_workflow_job(connection, "trigger-worker", lease_seconds=60)
        assert job is not None
        assert job.id == ready_result.job_id
        assert PullRequestEvent.from_payload(job.payload) == ready
        run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=ready.base_sha,
            head_sha=ready.head_sha,
            model="openai/test-review-model",
            prompt_version="native-review-v2-repository-policy",
            context_fingerprint="d" * 64,
        )
        persist_review_report(connection, run.id, skipped_report)
        assert load_review_report(connection, run.id) == skipped_report

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM workflow_jobs
                WHERE id = %s
                """,
                (draft_result.job_id,),
            )
            draft_status = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT
                    is_draft,
                    labels,
                    author,
                    base_branch,
                    head_branch,
                    title
                FROM pull_requests
                WHERE id = %s
                """,
                (job.pull_request_id,),
            )
            stored_metadata = cursor.fetchone()
            cursor.execute(
                "SELECT status, skip_reason FROM review_runs WHERE id = %s",
                (run.id,),
            )
            run_state = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM review_publications WHERE review_run_id = %s",
                (run.id,),
            )
            publication_count = cursor.fetchone()[0]
        connection.rollback()

    assert draft_status == "superseded"
    assert stored_metadata == (
        False,
        ["needs-review"],
        "octocat",
        "main",
        "feature/trigger-policy",
        "Add trigger policy",
    )
    assert run_state == ("skipped", "automatic_disabled")
    assert publication_count == 0


def test_finding_lineage_addresses_and_reopens_one_durable_thread():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "workflow/finding-lineage"

    def event(
        *,
        head: str,
        delivery: str,
        updated_at: str,
    ) -> PullRequestEvent:
        return PullRequestEvent.from_payload(
            {
                "provider": "github",
                "scm_base_url": "https://github.com",
                "api_base_url": "https://api.github.com",
                "repo_full_name": repo,
                "number": 44,
                "web_url": f"https://github.com/{repo}/pull/44",
                "action": "synchronize",
                "head_sha": head,
                "base_sha": "0" * 40,
                "updated_at": updated_at,
                "delivery_id": delivery,
            }
        )

    def finding(fingerprint: str, line: int) -> ReviewFinding:
        return ReviewFinding(
            fingerprint=fingerprint,
            title="Validate the tenant boundary",
            body="The changed handler forwards an unscoped tenant ID.",
            severity=Severity.HIGH,
            category=Category.SECURITY,
            confidence=0.92,
            file_path="service/api.py",
            line=line,
            side="RIGHT",
            evidence="The forwarded value is accepted directly from the request.",
            suggested_fix="Check tenant ownership before forwarding the value.",
        )

    def report(findings: list[ReviewFinding]) -> ReviewReport:
        return ReviewReport(
            summary="Review continuity integration fixture.",
            risk_score=7 if findings else 0,
            findings=findings,
            diff_file_count=1,
            reviewed_file_count=1,
            context_chunk_count=0,
            prompt_tokens=10,
            completion_tokens=2,
        )

    first_event = event(
        head="1" * 40,
        delivery="lineage-1",
        updated_at="2026-07-23T19:00:00Z",
    )
    second_event = event(
        head="2" * 40,
        delivery="lineage-2",
        updated_at="2026-07-23T19:01:00Z",
    )
    third_event = event(
        head="3" * 40,
        delivery="lineage-3",
        updated_at="2026-07-23T19:02:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )

        first_queued = enqueue_review_event(
            connection,
            first_event,
            payload_sha256="1" * 64,
        )
        first_job = claim_workflow_job(
            connection,
            "lineage-worker-1",
            lease_seconds=60,
        )
        assert first_job is not None
        assert first_job.id == first_queued.job_id
        first_run = begin_review_run(
            connection,
            workflow_job_id=first_job.id,
            repository_id=repository.id,
            pull_request_id=first_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=first_event.base_sha,
            head_sha=first_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="1" * 64,
        )
        first_finding = finding("a" * 64, 20)
        persist_review_report(
            connection,
            first_run.id,
            report([first_finding]),
        )
        first_continuity = load_review_continuity(connection, first_run.id)
        assert first_continuity.new_fingerprints == (first_finding.fingerprint,)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM finding_lineages WHERE pull_request_id = %s",
                (first_job.pull_request_id,),
            )
            assert cursor.fetchone()[0] == "pending"
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider="github",
            comments=(
                PublishedFindingComment(
                    fingerprint=first_finding.fingerprint,
                    external_id="501",
                    external_node_id="PRRC_501",
                    external_url="https://example/comment/501",
                ),
            ),
        )
        first_publication = begin_publication(
            connection,
            first_run.id,
            scm_provider="github",
        )
        assert first_publication.review_number == 1
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="review-1",
            external_url="https://example/review/1",
        )
        assert complete_workflow_job(
            connection,
            first_job.id,
            "lineage-worker-1",
        )

        second_queued = enqueue_review_event(
            connection,
            second_event,
            payload_sha256="2" * 64,
        )
        second_job = claim_workflow_job(
            connection,
            "lineage-worker-2",
            lease_seconds=60,
        )
        assert second_job is not None
        assert second_job.id == second_queued.job_id
        second_run = begin_review_run(
            connection,
            workflow_job_id=second_job.id,
            repository_id=repository.id,
            pull_request_id=second_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=second_event.base_sha,
            head_sha=second_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="2" * 64,
        )
        assert (
            latest_published_review_head(
                connection,
                pull_request_id=second_job.pull_request_id,
                before_review_run_id=second_run.id,
            )
            == first_event.head_sha
        )
        persist_review_report(
            connection,
            second_run.id,
            report([]),
            touched_paths=frozenset({"service/api.py"}),
        )
        second_continuity = load_review_continuity(connection, second_run.id)
        assert [item.title for item in second_continuity.addressed] == [
            first_finding.title
        ]
        assert second_continuity.open_findings == ()
        second_publication = begin_publication(
            connection,
            second_run.id,
            scm_provider="github",
        )
        assert second_publication.review_number == 2
        mark_publication_published(
            connection,
            second_publication.id,
            external_id="review-2",
            external_url="https://example/review/2",
        )
        address_operations = begin_thread_operations(
            connection,
            review_run_id=second_run.id,
        )
        assert len(address_operations) == 1
        assert address_operations[0].kind == "address"
        mark_thread_operation_published(
            connection,
            address_operations[0].id,
            result=PublishedThreadOperation(
                external_reply_id="601",
                external_reply_url="https://example/comment/601",
                thread_node_id="PRRT_501",
            ),
        )
        assert (
            begin_thread_operations(
                connection,
                review_run_id=second_run.id,
            )
            == ()
        )
        assert complete_workflow_job(
            connection,
            second_job.id,
            "lineage-worker-2",
        )

        third_queued = enqueue_review_event(
            connection,
            third_event,
            payload_sha256="3" * 64,
        )
        third_job = claim_workflow_job(
            connection,
            "lineage-worker-3",
            lease_seconds=60,
        )
        assert third_job is not None
        assert third_job.id == third_queued.job_id
        third_run = begin_review_run(
            connection,
            workflow_job_id=third_job.id,
            repository_id=repository.id,
            pull_request_id=third_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=third_event.base_sha,
            head_sha=third_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="3" * 64,
        )
        reopened_finding = finding("b" * 64, 27)
        persist_review_report(
            connection,
            third_run.id,
            report([reopened_finding]),
            touched_paths=frozenset({"service/api.py"}),
        )
        third_continuity = load_review_continuity(connection, third_run.id)
        assert third_continuity.reopened_fingerprints == (
            reopened_finding.fingerprint,
        )
        third_publication = begin_publication(
            connection,
            third_run.id,
            scm_provider="github",
        )
        assert third_publication.review_number == 3
        mark_publication_published(
            connection,
            third_publication.id,
            external_id="review-3",
            external_url="https://example/review/3",
        )
        reopen_operations = begin_thread_operations(
            connection,
            review_run_id=third_run.id,
        )
        assert len(reopen_operations) == 1
        assert reopen_operations[0].kind == "reopen"
        mark_thread_operation_published(
            connection,
            reopen_operations[0].id,
            result=PublishedThreadOperation(
                external_reply_id="602",
                external_reply_url="https://example/comment/602",
                thread_node_id="PRRT_501",
            ),
        )
        assert complete_workflow_job(
            connection,
            third_job.id,
            "lineage-worker-3",
        )

        fourth_event = event(
            head="4" * 40,
            delivery="lineage-4",
            updated_at="2026-07-23T19:03:00Z",
        )
        fourth_queued = enqueue_review_event(
            connection,
            fourth_event,
            payload_sha256="4" * 64,
        )
        fourth_job = claim_workflow_job(
            connection,
            "lineage-worker-4",
            lease_seconds=60,
        )
        assert fourth_job is not None
        assert fourth_job.id == fourth_queued.job_id
        fourth_run = begin_review_run(
            connection,
            workflow_job_id=fourth_job.id,
            repository_id=repository.id,
            pull_request_id=fourth_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=fourth_event.base_sha,
            head_sha=fourth_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="4" * 64,
        )
        abandoned_finding = finding("c" * 64, 5).model_copy(
            update={
                "file_path": "service/new.py",
                "title": "Remove the abandoned path",
            }
        )
        persist_review_report(
            connection,
            fourth_run.id,
            report([abandoned_finding]),
            touched_paths=frozenset({"service/new.py"}),
        )
        mark_review_superseded(connection, fourth_run.id)
        assert supersede_workflow_job(
            connection,
            fourth_job.id,
            "lineage-worker-4",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, first_seen_review_run_id, last_seen_review_run_id
                FROM finding_lineages
                WHERE pull_request_id = %s
                """,
                (third_job.pull_request_id,),
            )
            lineage_state = cursor.fetchone()
            cursor.execute(
                """
                SELECT transition
                FROM finding_lineage_events
                ORDER BY id
                """
            )
            transitions = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT status, thread_node_id FROM finding_threads"
            )
            thread_state = cursor.fetchone()
            cursor.execute(
                """
                SELECT operation_kind, status, attempt_count
                FROM finding_thread_operations
                ORDER BY id
                """
            )
            operation_states = cursor.fetchall()
            cursor.execute(
                """
                SELECT count(*)
                FROM finding_lineages
                WHERE status = 'pending'
                """
            )
            pending_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT signal_kind, suppression_protected
                FROM review_feedback_events
                WHERE source_kind = 'commit_outcome'
                ORDER BY id
                """
            )
            outcome_signals = cursor.fetchall()
        connection.rollback()

    assert lineage_state == ("active", first_run.id, third_run.id)
    assert transitions == ["new", "addressed", "reopened"]
    assert thread_state == ("active", "PRRT_501")
    assert operation_states == [
        ("address", "published", 1),
        ("reopen", "published", 1),
    ]
    assert outcome_signals == [
        ("addressed", True),
        ("reopened", True),
    ]
    assert pending_count == 0


def _unanchored_lineage_fixture_finding(
    fingerprint: str,
    *,
    title: str,
    path: str,
    line: int,
) -> ReviewFinding:
    return ReviewFinding(
        fingerprint=fingerprint,
        title=title,
        body=f"The changed handler in {path} accepts an unscoped tenant ID.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.91,
        file_path=path,
        line=line,
        side="RIGHT",
        evidence=f"Line {line} forwards the request value without a check.",
        suggested_fix="Check tenant ownership before forwarding the value.",
    )


def _unanchored_lineage_fixture_report(findings: list[ReviewFinding]) -> ReviewReport:
    return ReviewReport(
        summary="Inline attach regression fixture.",
        risk_score=7 if findings else 0,
        findings=findings,
        diff_file_count=2,
        reviewed_file_count=2,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )


def _threadless_active_lineage_count(cursor, pull_request_id: int) -> int:
    cursor.execute(
        """
        SELECT count(*)
        FROM finding_lineages AS lineage
        WHERE lineage.pull_request_id = %s
          AND lineage.status = 'active'
          AND NOT EXISTS (
              SELECT 1
              FROM finding_threads AS thread
              WHERE thread.lineage_id = lineage.id
          )
        """,
        (pull_request_id,),
    )
    return int(cursor.fetchone()[0])


def _assert_failed_inline_attach_never_activates_threadless_lineage(
    *,
    provider: str,
    repo: str,
    scm_base_url: str,
    api_base_url: str,
    web_url: str,
    number: int,
    attached_root_comment_id: str,
    retried_root_comment_id: str,
) -> None:
    """One partially attached publication must not strand a `new` lineage open."""
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]

    def event(*, head: str, delivery: str, updated_at: str) -> PullRequestEvent:
        return PullRequestEvent.from_payload(
            {
                "provider": provider,
                "scm_base_url": scm_base_url,
                "api_base_url": api_base_url,
                "repo_full_name": repo,
                "number": number,
                "web_url": web_url,
                "action": "synchronize",
                "head_sha": head,
                "base_sha": "0" * 40,
                "updated_at": updated_at,
                "delivery_id": delivery,
            }
        )

    attached = _unanchored_lineage_fixture_finding(
        "a" * 64,
        title="Validate the tenant boundary",
        path="service/attached.py",
        line=12,
    )
    unattached = _unanchored_lineage_fixture_finding(
        "b" * 64,
        title="Reject the unscoped identifier",
        path="service/unattached.py",
        line=44,
    )

    first_event = event(
        head="1" * 40,
        delivery=f"unanchored-{provider}-1",
        updated_at="2026-07-24T09:00:00Z",
    )
    second_event = event(
        head="2" * 40,
        delivery=f"unanchored-{provider}-2",
        updated_at="2026-07-24T09:01:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider=provider,
            scm_base_url=scm_base_url,
            full_name=repo,
            default_branch="main",
        )
        first_queued = enqueue_review_event(
            connection,
            first_event,
            payload_sha256="1" * 64,
        )
        first_job = claim_workflow_job(
            connection,
            f"unanchored-{provider}-worker-1",
            lease_seconds=60,
        )
        assert first_job is not None
        assert first_job.id == first_queued.job_id
        first_run = begin_review_run(
            connection,
            workflow_job_id=first_job.id,
            repository_id=repository.id,
            pull_request_id=first_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=first_event.base_sha,
            head_sha=first_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="1" * 64,
        )
        persist_review_report(
            connection,
            first_run.id,
            _unanchored_lineage_fixture_report([attached, unattached]),
        )
        first_continuity = load_review_continuity(connection, first_run.id)
        assert sorted(first_continuity.new_fingerprints) == sorted(
            (attached.fingerprint, unattached.fingerprint)
        )

        # The provider attached one inline root comment and rejected the other,
        # so the publication falls back to summary text for the rejected finding.
        attached_comment = PublishedFindingComment(
            fingerprint=attached.fingerprint,
            external_id=attached_root_comment_id,
            external_node_id=None,
            external_url=f"{web_url}#note_{attached_root_comment_id}",
            thread_id=None,
        )
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider=provider,
            comments=(attached_comment,),
        )
        first_publication = begin_publication(
            connection,
            first_run.id,
            scm_provider=provider,
        )
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="published-review-1",
            external_url=f"{web_url}#review-1",
            unanchored_fingerprints=frozenset({unattached.fingerprint}),
        )

        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                first_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT
                    finding.fingerprint,
                    lineage.status,
                    event.applied_at IS NOT NULL AS applied
                FROM finding_lineage_events AS event
                JOIN finding_lineages AS lineage ON lineage.id = event.lineage_id
                JOIN review_findings AS finding ON finding.id = event.finding_id
                WHERE event.review_run_id = %s
                ORDER BY finding.fingerprint
                """,
                (first_run.id,),
            )
            assert cursor.fetchall() == [
                (attached.fingerprint, "active", True),
                (unattached.fingerprint, "pending", False),
            ]

        # Re-entrancy: replaying the same publication must not duplicate the
        # anchor, activate the withheld lineage, or change any durable state.
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider=provider,
            comments=(attached_comment,),
        )
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="published-review-1",
            external_url=f"{web_url}#review-1",
            unanchored_fingerprints=frozenset({unattached.fingerprint}),
        )
        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                first_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT root_comment_id)
                FROM finding_threads AS thread
                JOIN finding_lineages AS lineage ON lineage.id = thread.lineage_id
                WHERE lineage.pull_request_id = %s
                """,
                (first_job.pull_request_id,),
            )
            assert cursor.fetchone() == (1, 1)
        assert complete_workflow_job(
            connection,
            first_job.id,
            f"unanchored-{provider}-worker-1",
        )

        # The next review re-derives the withheld finding as `new`, so the
        # inline attach is retried instead of being lost forever.
        second_queued = enqueue_review_event(
            connection,
            second_event,
            payload_sha256="2" * 64,
        )
        second_job = claim_workflow_job(
            connection,
            f"unanchored-{provider}-worker-2",
            lease_seconds=60,
        )
        assert second_job is not None
        assert second_job.id == second_queued.job_id
        second_run = begin_review_run(
            connection,
            workflow_job_id=second_job.id,
            repository_id=repository.id,
            pull_request_id=second_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=second_event.base_sha,
            head_sha=second_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="2" * 64,
        )
        persist_review_report(
            connection,
            second_run.id,
            _unanchored_lineage_fixture_report([attached, unattached]),
            touched_paths=frozenset({attached.file_path, unattached.file_path}),
        )
        second_continuity = load_review_continuity(connection, second_run.id)
        assert second_continuity.new_fingerprints == (unattached.fingerprint,)
        assert second_continuity.persistent_fingerprints == (attached.fingerprint,)
        assert unattached.fingerprint in second_continuity.inline_fingerprints

        record_finding_threads(
            connection,
            review_run_id=second_run.id,
            scm_provider=provider,
            comments=(
                PublishedFindingComment(
                    fingerprint=unattached.fingerprint,
                    external_id=retried_root_comment_id,
                    external_node_id=None,
                    external_url=f"{web_url}#note_{retried_root_comment_id}",
                    thread_id=None,
                ),
            ),
        )
        second_publication = begin_publication(
            connection,
            second_run.id,
            scm_provider=provider,
        )
        mark_publication_published(
            connection,
            second_publication.id,
            external_id="published-review-2",
            external_url=f"{web_url}#review-2",
        )

        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                second_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT lineage.status, thread.root_comment_id
                FROM finding_lineages AS lineage
                JOIN finding_threads AS thread ON thread.lineage_id = lineage.id
                WHERE lineage.pull_request_id = %s
                ORDER BY thread.root_comment_id
                """,
                (second_job.pull_request_id,),
            )
            anchored = cursor.fetchall()
            cursor.execute(
                "SELECT count(*) FROM finding_lineages WHERE pull_request_id = %s",
                (second_job.pull_request_id,),
            )
            lineage_count = int(cursor.fetchone()[0])
        connection.rollback()

    # The withheld lineage was reused rather than duplicated by the retry.
    assert lineage_count == 2
    assert anchored == [
        ("active", attached_root_comment_id),
        ("active", retried_root_comment_id),
    ]


def test_github_failed_inline_attach_never_activates_a_threadless_lineage():
    _assert_failed_inline_attach_never_activates_threadless_lineage(
        provider="github",
        repo="workflow/unanchored-github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        web_url="https://github.com/workflow/unanchored-github/pull/71",
        number=71,
        attached_root_comment_id="710",
        retried_root_comment_id="711",
    )


def test_review_conversations_are_durable_ordered_and_retryable():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "workflow/review-conversation"
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

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )
        queued_review = enqueue_review_event(
            connection,
            review_event,
            payload_sha256="a" * 64,
        )
        review_job = claim_workflow_job(
            connection,
            "conversation-review-worker",
            lease_seconds=60,
        )
        assert review_job is not None
        assert review_job.id == queued_review.job_id
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
        assert complete_workflow_job(
            connection,
            review_job.id,
            "conversation-review-worker",
        )

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
        assert complete_workflow_job(
            connection,
            feedback_job.id,
            "feedback-worker-1",
        )

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
        second_feedback_event = FeedbackSyncEvent.from_payload(
            feedback_retry.payload
        )
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
        assert complete_workflow_job(
            connection,
            feedback_retry.id,
            "feedback-worker-2",
        )
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
        duplicate = enqueue_review_conversation_event(
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
        assert duplicate.job_id == first.job_id
        assert duplicate.state == "duplicate_delivery:queued"

        first_job = claim_workflow_job(
            connection,
            "conversation-worker-1",
            lease_seconds=60,
        )
        assert first_job is not None
        assert first_job.id == first.job_id
        assert (
            fail_workflow_job(
                connection,
                first_job.id,
                "conversation-worker-1",
                "conversation_retry",
                retryable=True,
            )
            == "queued"
        )
        assert (
            claim_workflow_job(
                connection,
                "conversation-worker-2",
                lease_seconds=60,
            )
            is None
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_jobs SET available_at = now() WHERE id = %s",
                (first_job.id,),
            )
        first_retry = claim_workflow_job(
            connection,
            "conversation-worker-1",
            lease_seconds=60,
        )
        assert first_retry is not None
        assert first_retry.id == first.job_id

        first_work = begin_conversation_generation(
            connection,
            workflow_job_id=first_retry.id,
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
            workflow_job_id=first_retry.id,
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
        assert complete_workflow_job(
            connection,
            first_retry.id,
            "conversation-worker-1",
        )

        second_job = claim_workflow_job(
            connection,
            "conversation-worker-2",
            lease_seconds=60,
        )
        assert second_job is not None
        assert second_job.id == second.job_id
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
        assert complete_workflow_job(
            connection,
            second_job.id,
            "conversation-worker-2",
        )

        unrelated = enqueue_review_conversation_event(
            connection,
            conversation(
                comment_id="803",
                delivery_id="conversation-unrelated",
                question="Please answer this unrelated thread.",
                root_comment_id="999",
            ),
            payload_sha256="e" * 64,
        )
        assert unrelated.job_id is None
        assert unrelated.state == "ignored:not_diffuse_thread"

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
            cursor.execute(
                """
                SELECT status, attempt_count
                FROM workflow_jobs
                WHERE job_type = 'answer_review_comment'
                ORDER BY id
                """
            )
            job_states = cursor.fetchall()
        connection.rollback()

    assert message_states == [
        ("published", 2, "901", None),
        ("ignored", 0, None, "conversation_disabled"),
    ]
    assert job_states == [
        ("succeeded", 2),
        ("succeeded", 1),
    ]


def test_suggested_rules_are_evidence_bound_moderated_and_review_snapshotted():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "workflow/suggested-rules"
    review_event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repo,
            "number": 71,
            "web_url": f"https://github.com/{repo}/pull/71",
            "action": "opened",
            "head_sha": "7" * 40,
            "base_sha": "6" * 40,
            "updated_at": "2026-07-23T21:00:00Z",
            "delivery_id": "suggested-rule-review",
        }
    )
    finding = ReviewFinding(
        fingerprint="e" * 64,
        title="Use the shared tenant guard",
        body="The handler performs an inline tenant check instead of the shared guard.",
        severity=Severity.MEDIUM,
        category=Category.ARCHITECTURE,
        confidence=0.91,
        file_path="service/accounts.py",
        line=18,
        side="RIGHT",
        evidence="The changed handler duplicates the tenant boundary.",
        suggested_fix="Call require_tenant_access before loading the account.",
    )
    report = ReviewReport(
        summary="One architecture issue.",
        risk_score=4,
        findings=[finding],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=8,
        completion_tokens=2,
    )

    def feedback(comment_id: str, delivery_id: str, body: str):
        return ReviewFeedbackCommentEvent(
            provider="github",
            scm_base_url="https://github.com",
            api_base_url="https://api.github.com",
            repo_full_name=repo,
            number=71,
            delivery_id=delivery_id,
            external_comment_id=comment_id,
            root_comment_id="1701",
            author="maintainer",
            author_association="MEMBER",
            created_at="2026-07-23T21:01:00Z",
            body=body,
            file_path=finding.file_path,
        )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )
        queued = enqueue_review_event(
            connection,
            review_event,
            payload_sha256="a" * 64,
        )
        review_job = claim_workflow_job(
            connection,
            "suggested-rule-review-worker",
            lease_seconds=60,
        )
        assert review_job is not None and review_job.id == queued.job_id
        review_run = begin_review_run(
            connection,
            workflow_job_id=review_job.id,
            repository_id=repository.id,
            pull_request_id=review_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=review_event.base_sha,
            head_sha=review_event.head_sha,
            model="openai/test-review-model",
            prompt_version="suggested-rule-fixture-v1",
            context_fingerprint="a" * 64,
        )
        persist_review_report(connection, review_run.id, report)
        record_finding_threads(
            connection,
            review_run_id=review_run.id,
            scm_provider="github",
            comments=(
                PublishedFindingComment(
                    fingerprint=finding.fingerprint,
                    external_id="1701",
                    external_node_id="PRRC_1701",
                    external_url="https://example/comment/1701",
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
            external_id="suggested-rule-review-1",
            external_url="https://example/review/suggested-rule-1",
        )
        assert complete_workflow_job(
            connection,
            review_job.id,
            "suggested-rule-review-worker",
        )

        assert (
            record_review_comment_feedback(
                connection,
                feedback(
                    "1751",
                    "suggested-rule-feedback-1",
                    "Please always use our shared tenant guard in account handlers.",
                ),
                payload_sha256="1" * 64,
            )
            == "recorded"
        )
        assert schedule_due_rule_learning_jobs(
            connection,
            minimum_evidence=1,
            minimum_pull_requests=1,
            evaluation_interval_seconds=60,
            limit=5,
        ) == 1
        stale_job = claim_workflow_job(
            connection,
            "suggested-rule-stale-worker",
            lease_seconds=60,
        )
        assert stale_job is not None

        assert (
            record_review_comment_feedback(
                connection,
                feedback(
                    "1752",
                    "suggested-rule-feedback-2",
                    "Use require_tenant_access here, as in our other handlers.",
                ),
                payload_sha256="2" * 64,
            )
            == "recorded"
        )
        stale_work = begin_rule_learning(
            connection,
            workflow_job_id=stale_job.id,
            event=RuleLearningJobEvent.from_payload(stale_job.payload),
            model="openai/test-learning",
            prompt_version="suggested-rules-test-v1",
        )
        assert stale_work.status == "stale"
        assert complete_workflow_job(
            connection,
            stale_job.id,
            "suggested-rule-stale-worker",
        )

        generation_queue = queue_rule_learning_job(
            connection,
            repository_id=repository.id,
            minimum_evidence=2,
            minimum_pull_requests=1,
            evaluation_interval_seconds=60,
        )
        assert generation_queue.accepted
        generation_job = claim_workflow_job(
            connection,
            "suggested-rule-generation-worker",
            lease_seconds=60,
        )
        assert generation_job is not None
        work = begin_rule_learning(
            connection,
            workflow_job_id=generation_job.id,
            event=RuleLearningJobEvent.from_payload(generation_job.payload),
            model="openai/test-learning",
            prompt_version="suggested-rules-test-v1",
        )
        assert work.needs_generation
        assert len(work.evidence) == 2
        candidate = SuggestedRuleCandidate(
            title="Use the shared tenant guard",
            guidance=(
                "Account handlers must call require_tenant_access before loading an account."
            ),
            applies_to=("service/accounts.py",),
            severity="high",
            category="architecture",
            evidence_event_ids=tuple(item.event_id for item in work.evidence),
        )
        reject_candidate = candidate.model_copy(
            update={
                "title": "Wrap account errors",
                "guidance": "Account handlers must return the shared structured error shape.",
                "category": "reliability",
            }
        )
        invalid_candidate = candidate.model_copy(
            update={
                "title": "Uncited rule",
                "guidance": "This model output cites evidence that does not exist.",
                "evidence_event_ids": (999_999,),
            }
        )
        generated = persist_rule_suggestions(
            connection,
            work=work,
            suggestions=SuggestedRuleBatch(
                suggestions=(candidate, reject_candidate, invalid_candidate)
            ),
            minimum_support=2,
            minimum_support_pull_requests=1,
            prompt_tokens=15,
            completion_tokens=5,
        )
        assert (
            generated.proposed,
            generated.consolidated,
            generated.rejected_candidates,
        ) == (2, 0, 1)
        assert complete_workflow_job(
            connection,
            generation_job.id,
            "suggested-rule-generation-worker",
        )

        suggestions = list_learned_rules(
            connection,
            repository_id=repository.id,
        )
        assert len(suggestions) == 2
        suggestion = next(
            item for item in suggestions if item.title == "Use the shared tenant guard"
        )
        rejected_suggestion = next(
            item for item in suggestions if item.title == "Wrap account errors"
        )
        assert suggestion.status == "suggested"
        assert suggestion.evidence_count == 2
        audit = load_learned_rule_audit(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
        )
        assert len(audit["evidence"]) == 2
        assert [event["action"] for event in audit["history"]] == ["proposed"]
        rejected_rule = moderate_learned_rule(
            connection,
            repository_id=repository.id,
            learned_rule_id=rejected_suggestion.id,
            action="reject",
            actor_login="operator@example.invalid",
            actor_authority="OPERATOR",
            event_key="suggested-rule-rejection-1",
            expected_version=1,
            reason="This is not actually a repeated team standard.",
        )
        assert rejected_rule.status == "rejected"
        with pytest.raises(ValueError, match="not authorized"):
            moderate_learned_rule(
                connection,
                repository_id=repository.id,
                learned_rule_id=suggestion.id,
                action="approve",
                actor_login="outside-user",
                actor_authority="OUTSIDER",
                event_key="suggested-rule-unauthorized-1",
                expected_version=1,
            )

        edited = moderate_learned_rule(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
            action="edit",
            actor_login="operator@example.invalid",
            actor_authority="OPERATOR",
            event_key="suggested-rule-edit-1",
            expected_version=1,
            guidance=(
                "All account handlers must call require_tenant_access before loading "
                "or mutating an account."
            ),
        )
        assert edited.version == 2
        active = moderate_learned_rule(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
            action="approve",
            actor_login="operator@example.invalid",
            actor_authority="OPERATOR",
            event_key="suggested-rule-approval-1",
            expected_version=2,
        )
        assert active.status == "active"
        assert (
            moderate_learned_rule(
                connection,
                repository_id=repository.id,
                learned_rule_id=suggestion.id,
                action="approve",
                actor_login="operator@example.invalid",
                actor_authority="OPERATOR",
                event_key="suggested-rule-approval-1",
                expected_version=2,
            )
            == active
        )
        approved = load_active_learned_rules(
            connection,
            repository_id=repository.id,
        )
        assert len(approved) == 1
        assert approved[0].version == 2

        assert (
            record_review_comment_feedback(
                connection,
                feedback(
                    "1753",
                    "suggested-rule-feedback-3",
                    "The shared tenant guard is required for account mutations too.",
                ),
                payload_sha256="3" * 64,
            )
            == "recorded"
        )
        refresh_queue = queue_rule_learning_job(
            connection,
            repository_id=repository.id,
            minimum_evidence=3,
            minimum_pull_requests=1,
            evaluation_interval_seconds=60,
        )
        assert refresh_queue.accepted
        refresh_job = claim_workflow_job(
            connection,
            "suggested-rule-refresh-worker",
            lease_seconds=60,
        )
        assert refresh_job is not None
        refresh_work = begin_rule_learning(
            connection,
            workflow_job_id=refresh_job.id,
            event=RuleLearningJobEvent.from_payload(refresh_job.payload),
            model="openai/test-learning",
            prompt_version="suggested-rules-test-v1",
        )
        refresh_candidate = candidate.model_copy(
            update={
                "guidance": edited.guidance,
                "evidence_event_ids": tuple(
                    item.event_id for item in refresh_work.evidence
                ),
            }
        )
        refreshed = persist_rule_suggestions(
            connection,
            work=refresh_work,
            suggestions=SuggestedRuleBatch(suggestions=(refresh_candidate,)),
            minimum_support=3,
            minimum_support_pull_requests=1,
            prompt_tokens=16,
            completion_tokens=4,
        )
        assert (refreshed.proposed, refreshed.consolidated) == (0, 1)
        assert complete_workflow_job(
            connection,
            refresh_job.id,
            "suggested-rule-refresh-worker",
        )
        refreshed_rule = next(
            item
            for item in list_learned_rules(
                connection,
                repository_id=repository.id,
            )
            if item.id == suggestion.id
        )
        assert refreshed_rule.evidence_count == 3

        inactive = moderate_learned_rule(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
            action="deactivate",
            actor_login="operator@example.invalid",
            actor_authority="OPERATOR",
            event_key="suggested-rule-deactivate-1",
            expected_version=2,
            reason="Temporarily verify the scope.",
        )
        assert inactive.status == "inactive"
        assert (
            load_active_learned_rules(
                connection,
                repository_id=repository.id,
            )
            == ()
        )
        reactivated = moderate_learned_rule(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
            action="reactivate",
            actor_login="operator@example.invalid",
            actor_authority="OPERATOR",
            event_key="suggested-rule-reactivate-1",
            expected_version=2,
        )
        assert reactivated.status == "active"

        next_review_event = PullRequestEvent.from_payload(
            {
                **review_event.to_payload(),
                "action": "synchronize",
                "head_sha": "8" * 40,
                "updated_at": "2026-07-23T21:05:00Z",
                "delivery_id": "suggested-rule-review-2",
            }
        )
        next_queued = enqueue_review_event(
            connection,
            next_review_event,
            payload_sha256="b" * 64,
        )
        next_job = claim_workflow_job(
            connection,
            "suggested-rule-review-worker-2",
            lease_seconds=60,
        )
        assert next_job is not None and next_job.id == next_queued.job_id
        custom_context = ApprovedCustomContext(
            id=87,
            context_type="CUSTOM_INSTRUCTION",
            body="Treat account mutation handlers as tenant-boundary code.",
            applies_to=("service/accounts.py",),
            metadata={"source": "operator"},
        )
        next_run = begin_review_run(
            connection,
            workflow_job_id=next_job.id,
            repository_id=repository.id,
            pull_request_id=next_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=next_review_event.base_sha,
            head_sha=next_review_event.head_sha,
            model="openai/test-review-model",
            prompt_version="suggested-rule-fixture-v1",
            context_fingerprint="b" * 64,
            learned_rules=load_active_learned_rules(
                connection,
                repository_id=repository.id,
            ),
            custom_contexts=(custom_context,),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT learned_rule_id, rule_version, snapshot->>'guidance'
                FROM review_run_learned_rules
                WHERE review_run_id = %s
                """,
                (next_run.id,),
            )
            snapshotted_rule = cursor.fetchone()
            cursor.execute(
                """
                SELECT custom_context_id,
                       snapshot->>'context_type',
                       snapshot->>'body',
                       snapshot->'applies_to',
                       snapshot->'metadata'
                FROM review_run_custom_contexts
                WHERE review_run_id = %s
                """,
                (next_run.id,),
            )
            snapshotted_context = cursor.fetchone()
        assert snapshotted_rule == (
            suggestion.id,
            2,
            edited.guidance,
        )
        assert snapshotted_context == (
            custom_context.id,
            custom_context.context_type,
            custom_context.body,
            list(custom_context.applies_to),
            custom_context.metadata,
        )
        mark_review_superseded(connection, next_run.id)
        assert supersede_workflow_job(
            connection,
            next_job.id,
            "suggested-rule-review-worker-2",
        )

        final_audit = load_learned_rule_audit(
            connection,
            repository_id=repository.id,
            learned_rule_id=suggestion.id,
        )
        actions = [event["action"] for event in final_audit["history"]]
        connection.rollback()

    assert actions == [
        "proposed",
        "edited",
        "approved",
        "evidence_added",
        "deactivated",
        "reactivated",
    ]


def _numbered_event(*, number: int, delivery: str, head: str) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "workflow/concurrent-repo",
            "number": number,
            "web_url": f"https://github.com/workflow/concurrent-repo/pull/{number}",
            "action": "synchronize",
            "head_sha": head,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T16:00:00Z",
            "delivery_id": delivery,
        }
    )


def test_concurrent_workers_claim_independent_jobs_in_one_repository():
    """Claiming must lock only the job row, never the shared repository row.

    A bare `FOR UPDATE SKIP LOCKED` over the workflow_jobs/repositories join
    locks both tables, so one worker holding a job in a repository makes every
    other queued job in that repository invisible to its peers.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    with closing(psycopg2.connect(database_url)) as setup_connection:
        register_repository(
            setup_connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="workflow/concurrent-repo",
            default_branch="main",
        )
        first = enqueue_review_event(
            setup_connection,
            _numbered_event(
                number=41,
                delivery="concurrent-delivery-1",
                head="a" * 40,
            ),
            payload_sha256="1" * 64,
        )
        second = enqueue_review_event(
            setup_connection,
            _numbered_event(
                number=42,
                delivery="concurrent-delivery-2",
                head="c" * 40,
            ),
            payload_sha256="2" * 64,
        )
        # Both worker connections must see these rows, so unlike the
        # single-connection tests above this one commits and cleans up below.
        setup_connection.commit()

    try:
        assert first.accepted and second.accepted
        assert first.job_id != second.job_id

        with (
            closing(psycopg2.connect(database_url)) as worker_one,
            closing(psycopg2.connect(database_url)) as worker_two,
        ):
            # worker-1 holds an open transaction on its claimed job.
            claimed_one = claim_workflow_job(worker_one, "worker-1", lease_seconds=60)
            assert claimed_one is not None

            claimed_two = claim_workflow_job(worker_two, "worker-2", lease_seconds=60)
            assert claimed_two is not None, (
                "worker-2 was starved by worker-1's lock on the shared repository row"
            )
            assert {claimed_one.id, claimed_two.id} == {first.job_id, second.job_id}

            worker_one.rollback()
            worker_two.rollback()
    finally:
        with (
            closing(psycopg2.connect(database_url)) as cleanup_connection,
            cleanup_connection,
            cleanup_connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM repositories WHERE full_name = %s",
                ("workflow/concurrent-repo",),
            )
            # Deliveries are not owned by the repository row, so the unique
            # (provider, base_url, delivery_id) rows outlive it.
            cursor.execute(
                "DELETE FROM scm_webhook_deliveries WHERE delivery_id = ANY(%s)",
                (["concurrent-delivery-1", "concurrent-delivery-2"],),
            )
