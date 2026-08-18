"""PostgreSQL coverage for `diffuse.database.learning`.

Recovered from `tests/integration/test_workflow_postgres.py`. This is the only
place the learned-rule moderation state machine is driven end to end -- reject,
edit, approve, deactivate, reactivate, plus the two illegal transitions that
must raise -- and W4.2 says in as many words "do not change the approval
semantics." Without this, nothing holds those semantics in place.

`begin_rule_learning` is still keyed by `workflow_job_id`, so `_claimed_job` and
`_finish_job` are unavoidable scaffolding today; nothing here asserts on queue
behaviour.
"""

import os
from contextlib import closing

import psycopg2
import pytest
from diffuse.database.feedback import record_review_comment_feedback
from diffuse.database.finding import PublishedFindingComment, record_finding_threads
from diffuse.database.learning import (
    begin_rule_learning,
    list_learned_rules,
    load_active_learned_rules,
    load_learned_rule_audit,
    moderate_learned_rule,
    persist_rule_suggestions,
    queue_rule_learning_job,
    schedule_due_rule_learning_jobs,
)
from diffuse.database.review import (
    begin_publication,
    begin_review_run,
    mark_publication_published,
    mark_review_superseded,
    persist_review_report,
)
from diffuse.repository.learning_models import (
    RuleLearningJobEvent,
    SuggestedRuleBatch,
    SuggestedRuleCandidate,
)
from diffuse.repository.policy.resolve import ApprovedCustomContext
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import PullRequestEvent, ReviewFeedbackCommentEvent
from diffuse.review.workflow import (
    claim_workflow_job,
    complete_workflow_job,
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


def test_suggested_rules_are_evidence_bound_moderated_and_review_snapshotted():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "store/suggested-rules"
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
        review_job = _claimed_job(
            connection,
            review_event,
            payload_sha256="a" * 64,
            worker_id="suggested-rule-review-worker",
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
        _finish_job(connection, review_job.id, "suggested-rule-review-worker")

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
        _finish_job(connection, stale_job.id, "suggested-rule-stale-worker")

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
        _finish_job(connection, generation_job.id, "suggested-rule-generation-worker")

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
        _finish_job(connection, refresh_job.id, "suggested-rule-refresh-worker")
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
        next_job = _claimed_job(
            connection,
            next_review_event,
            payload_sha256="b" * 64,
            worker_id="suggested-rule-review-worker-2",
        )
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
        assert mark_review_superseded(
            connection,
            next_run.id,
            worker_id="suggested-rule-review-worker-2",
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

