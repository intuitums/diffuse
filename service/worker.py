"""Durable Diffuse review worker."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import time
from contextlib import closing
from functools import partial

import anyio

from indexer.embed import embedding_dimensions, embedding_model
from indexer.index_repo import index_repo
from indexer.store import get_conn
from repository_policy.models import RepositoryPolicySnapshot, validate_repo_path
from repository_policy.resolve import (
    PullRequestTriggerContext,
    ResolvedReviewPolicy,
    TriggerDecision,
    apply_approved_custom_contexts,
    apply_approved_learned_rules,
    evaluate_trigger,
    resolve_review_policy,
)
from repository_policy.store import load_repository_policy
from retriever.context_models import CrossRepositoryContextPlan
from retriever.retrieve import (
    compatible_snapshot_id,
    retrieve_context_from_plan,
    retrieve_context_from_snapshot,
)
from service.approval_publication import (
    ApprovalNotCurrentError,
    PublishedApproval,
)
from service.approval_store import (
    AutoApprovalHandle,
    begin_auto_approval,
    mark_auto_approval_cancelled,
    mark_auto_approval_failed,
    mark_auto_approval_published,
)
from service.auto_approval import AutoApprovalDecision, evaluate_auto_approval
from service.check_store import (
    CheckRunHandle,
    begin_check_run,
    get_check_run_for_workflow_job,
    mark_check_run_completed,
    mark_check_run_completing,
    mark_check_run_failed,
    mark_check_run_started,
)
from service.conversation_engine import (
    CONVERSATION_PROMPT_VERSION,
    build_conversation_retrieval_diff,
    conversation_model,
    generate_conversation_answer,
)
from service.conversation_store import (
    ConversationPublication,
    ConversationWork,
    begin_conversation_generation,
    begin_conversation_publication,
    mark_conversation_failed,
    mark_conversation_ignored,
    mark_conversation_published,
    mark_conversation_ready,
)
from service.cross_repository import resolve_cross_repository_context_plan
from service.custom_context_store import load_active_custom_contexts
from service.database_migrations import verify_database_current
from service.diff_parser import parse_unified_diff
from service.feedback_store import (
    FeedbackSyncTarget,
    begin_feedback_sync,
    mark_feedback_sync_failed,
    reconcile_review_reactions,
)
from service.finding_lineage import ReviewContinuity
from service.finding_store import (
    PublishedThreadOperation,
    ThreadOperationHandle,
    begin_thread_operations,
    latest_published_review_head,
    load_review_continuity,
    mark_thread_operation_failed,
    mark_thread_operation_published,
    record_finding_threads,
)
from service.github import fetch_pull_request_diff, fetch_pull_request_update_diff
from service.github_approval import (
    publish_github_approval,
)
from service.github_check import (
    complete_github_check_run,
    ensure_github_check_run,
    review_check_conclusion,
)
from service.github_conversation import publish_github_conversation_reply
from service.github_feedback import fetch_github_review_reactions
from service.github_review import PublishedReview, publish_github_review
from service.github_threads import apply_github_thread_operation
from service.gitlab_approval import publish_gitlab_approval
from service.gitlab_check import (
    complete_gitlab_check_run,
    ensure_gitlab_check_run,
)
from service.gitlab_conversation import publish_gitlab_conversation_reply
from service.gitlab_feedback import fetch_gitlab_review_reactions
from service.gitlab_review import (
    fetch_gitlab_merge_request_diff,
    fetch_gitlab_pull_request_update_diff,
    publish_gitlab_review,
)
from service.gitlab_threads import apply_gitlab_thread_operation
from service.learning_engine import (
    RULE_LEARNING_PROMPT_VERSION,
    generate_suggested_rules,
    rule_learning_model,
)
from service.learning_models import RuleLearningJobEvent, RuleLearningWork
from service.learning_store import (
    begin_rule_learning,
    load_active_learned_rules,
    mark_rule_learning_failed,
    persist_rule_suggestions,
    schedule_due_rule_learning_jobs,
)
from service.repositories import get_repository, update_mirror_state
from service.repository_mirror import RepositoryMirror, RepositoryMirrorError
from service.review_engine import (
    PROMPT_VERSION,
    generate_review,
    minimum_review_confidence,
    review_model,
    review_passes,
)
from service.review_models import ReviewFinding, ReviewReport
from service.review_store import (
    PublicationHandle,
    ReviewRunHandle,
    begin_publication,
    begin_review_run,
    load_review_report,
    mark_publication_failed,
    mark_publication_published,
    mark_review_failed,
    mark_review_superseded,
    mark_review_terminal_failed,
    persist_review_report,
)
from service.scm import (
    FeedbackSyncEvent,
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
)
from service.workflow import (
    WorkflowJob,
    claim_workflow_job,
    complete_workflow_job,
    fail_workflow_job,
    heartbeat_workflow_job,
    schedule_due_feedback_sync_jobs,
    supersede_workflow_job,
    workflow_job_is_current,
    workflow_job_is_latest,
)

LOGGER = logging.getLogger(__name__)


class ReviewSupersededError(RuntimeError):
    pass


class IndexSupersededError(RuntimeError):
    pass


async def _fetch_scm_pull_request_diff(event: PullRequestEvent) -> str:
    if event.provider == "github":
        return await fetch_pull_request_diff(event)
    if event.provider == "gitlab":
        return await fetch_gitlab_merge_request_diff(event)
    raise ValueError(f"Unsupported SCM provider: {event.provider}")


async def _fetch_scm_pull_request_update_diff(
    event: PullRequestEvent,
    previous_head_sha: str,
) -> str:
    if event.provider == "github":
        return await fetch_pull_request_update_diff(event, previous_head_sha)
    if event.provider == "gitlab":
        return await fetch_gitlab_pull_request_update_diff(
            event,
            previous_head_sha,
        )
    raise ValueError(f"Unsupported SCM provider: {event.provider}")


def _lease_seconds() -> int:
    value = int(os.environ.get("WORKFLOW_LEASE_SECONDS", "1800"))
    if value < 60:
        raise ValueError("WORKFLOW_LEASE_SECONDS must be at least 60")
    return value


def _claim(worker_id: str) -> WorkflowJob | None:
    with closing(get_conn()) as conn, conn:
        return claim_workflow_job(
            conn,
            worker_id,
            lease_seconds=_lease_seconds(),
        )


def _schedule_feedback_syncs() -> int:
    interval_seconds = int(os.environ.get("FEEDBACK_SYNC_INTERVAL_SECONDS", "900"))
    batch_size = int(os.environ.get("FEEDBACK_SYNC_BATCH_SIZE", "20"))
    api_base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    with closing(get_conn()) as conn, conn:
        return schedule_due_feedback_sync_jobs(
            conn,
            api_base_url=api_base_url,
            github_scm_base_url=os.environ.get(
                "GITHUB_WEB_URL",
                "https://github.com",
            ),
            gitlab_scm_base_url=os.environ.get(
                "GITLAB_WEB_URL",
                "https://gitlab.com",
            ),
            gitlab_api_base_url=os.environ.get(
                "GITLAB_API_URL",
                "https://gitlab.com/api/v4",
            ),
            interval_seconds=interval_seconds,
            limit=batch_size,
        )


def _schedule_rule_learning() -> int:
    minimum_evidence = int(os.environ.get("RULE_LEARNING_MIN_EVIDENCE", "10"))
    minimum_pull_requests = int(
        os.environ.get("RULE_LEARNING_MIN_PULL_REQUESTS", "10")
    )
    interval_seconds = int(
        os.environ.get("RULE_LEARNING_EVALUATION_INTERVAL_SECONDS", "3600")
    )
    batch_size = int(os.environ.get("RULE_LEARNING_BATCH_SIZE", "5"))
    with closing(get_conn()) as conn, conn:
        return schedule_due_rule_learning_jobs(
            conn,
            minimum_evidence=minimum_evidence,
            minimum_pull_requests=minimum_pull_requests,
            evaluation_interval_seconds=interval_seconds,
            limit=batch_size,
        )


def _heartbeat_and_check_current(job_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        heartbeat_workflow_job(
            conn,
            job_id,
            worker_id,
            lease_seconds=_lease_seconds(),
        )
        return workflow_job_is_current(conn, job_id, worker_id)


def _heartbeat_and_check_latest(job_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        if not heartbeat_workflow_job(
            conn,
            job_id,
            worker_id,
            lease_seconds=_lease_seconds(),
        ):
            return False
        return workflow_job_is_latest(conn, job_id, worker_id)


def _heartbeat_lease(job_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        return heartbeat_workflow_job(
            conn,
            job_id,
            worker_id,
            lease_seconds=_lease_seconds(),
        )


def _complete(job_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        return complete_workflow_job(conn, job_id, worker_id)


def _supersede(job_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        return supersede_workflow_job(conn, job_id, worker_id)


def _fail(job_id: int, worker_id: str, *, retryable: bool) -> str | None:
    with closing(get_conn()) as conn, conn:
        return fail_workflow_job(
            conn,
            job_id,
            worker_id,
            "workflow_processing_failed",
            retryable=retryable,
        )


def _set_mirror_state(
    repository_id: int,
    *,
    state: str,
    commit_sha: str | None = None,
    error_code: str | None = None,
) -> None:
    with closing(get_conn()) as conn, conn:
        update_mirror_state(
            conn,
            repository_id,
            state=state,
            commit_sha=commit_sha,
            error_code=error_code,
        )


def _begin_native_review(
    job: WorkflowJob,
    event: PullRequestEvent,
    context_plan: CrossRepositoryContextPlan,
    policy: ResolvedReviewPolicy,
) -> ReviewRunHandle:
    if job.pull_request_id is None:
        raise ValueError("Review job does not reference a pull request")
    with closing(get_conn()) as conn, conn:
        return begin_review_run(
            conn,
            workflow_job_id=job.id,
            repository_id=job.repository_id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=context_plan.primary_snapshot_id,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model=review_model(),
            prompt_version=PROMPT_VERSION,
            context_fingerprint=_review_context_fingerprint(
                context_plan,
                policy,
                event,
            ),
            learned_rules=policy.approved_learned_rules,
            custom_contexts=policy.approved_custom_contexts,
            context_snapshots=context_plan.related_snapshots,
        )


def _review_context_fingerprint(
    context_plan: CrossRepositoryContextPlan,
    policy: ResolvedReviewPolicy,
    event: PullRequestEvent,
) -> str:
    identity = "\0".join(
        (
            context_plan.fingerprint,
            policy.fingerprint,
            event.trigger_fingerprint,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _load_cross_repository_context_plan(
    repository_id: int,
    snapshot_id: int | None,
    policy: ResolvedReviewPolicy,
) -> CrossRepositoryContextPlan:
    with closing(get_conn()) as conn:
        return resolve_cross_repository_context_plan(
            conn,
            primary_repository_id=repository_id,
            primary_snapshot_id=snapshot_id,
            explicit_repositories=policy.context_repositories,
            model=embedding_model(),
            dimensions=embedding_dimensions(),
        )


def _load_review_policy(
    snapshot_id: int | None,
    diff_text: str,
    repository_id: int | None = None,
) -> ResolvedReviewPolicy:
    paths = tuple(
        path
        for file in parse_unified_diff(diff_text).files
        for path in (file.old_path, file.new_path)
        if path is not None
    )
    if snapshot_id is None:
        snapshot = RepositoryPolicySnapshot()
    else:
        with closing(get_conn()) as conn:
            snapshot = load_repository_policy(conn, snapshot_id)
    resolved = resolve_review_policy(
        snapshot,
        paths,
        default_passes=review_passes(),
        default_minimum_confidence=minimum_review_confidence(),
    )
    if repository_id is None:
        return resolved
    with closing(get_conn()) as conn:
        learned_rules = load_active_learned_rules(
            conn,
            repository_id=repository_id,
        )
        custom_contexts = load_active_custom_contexts(
            conn,
            repository_id=repository_id,
        )
    resolved = apply_approved_learned_rules(resolved, learned_rules)
    return apply_approved_custom_contexts(resolved, custom_contexts)


def _policy_filtered_diff(
    diff_text: str,
    policy: ResolvedReviewPolicy,
) -> str:
    return "\n\n".join(
        file.raw_text
        for file in parse_unified_diff(diff_text).files
        if file.comment_path and policy.allows_path(file.comment_path)
    )


def _trigger_decision(
    event: PullRequestEvent,
    _diff_text: str,
    policy: ResolvedReviewPolicy,
) -> TriggerDecision:
    return evaluate_trigger(
        policy,
        PullRequestTriggerContext(
            action=event.action,
            trigger_kind=event.trigger_kind,
            metadata_complete=event.metadata_complete,
            is_draft=event.is_draft,
            author=event.author,
            base_branch=event.base_branch,
            labels=event.labels,
            title=event.title,
            description=event.description,
            changed_file_count=event.changed_file_count,
        ),
    )


def _persist_trigger_skip(
    review_run_id: int,
    diff_text: str,
    policy: ResolvedReviewPolicy,
    decision: TriggerDecision,
) -> None:
    parsed = parse_unified_diff(diff_text)
    reviewable_count = sum(
        bool(file.comment_path and policy.allows_path(file.comment_path))
        for file in parsed.files
    )
    report = ReviewReport(
        summary=decision.message,
        risk_score=0,
        findings=[],
        diff_file_count=len(parsed.files),
        reviewed_file_count=0,
        ignored_file_count=len(parsed.files) - reviewable_count,
        inline_comments_enabled=False,
        publication_enabled=False,
        skip_reason=decision.reason_code,
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )
    with closing(get_conn()) as conn, conn:
        persist_review_report(conn, review_run_id, report)


def _generate_and_persist_review(
    job: WorkflowJob,
    review_run_id: int,
    diff_text: str,
    contexts: list,
    worker_id: str,
    policy: ResolvedReviewPolicy,
    touched_paths: frozenset[str],
) -> None:
    def report_progress() -> None:
        if not _heartbeat_and_check_current(job.id, worker_id):
            raise ReviewSupersededError(
                "A newer pull-request revision or worker lease replaced this review"
            )

    try:
        report = generate_review(
            diff_text,
            contexts,
            progress_callback=report_progress,
            policy=policy,
        )
        report_progress()
        with closing(get_conn()) as conn, conn:
            persist_review_report(
                conn,
                review_run_id,
                report,
                touched_paths=touched_paths,
            )
    except ReviewSupersededError:
        with closing(get_conn()) as conn, conn:
            mark_review_superseded(conn, review_run_id)
        raise
    except Exception:
        with closing(get_conn()) as conn, conn:
            mark_review_failed(conn, review_run_id)
        raise


def _load_native_report(review_run_id: int) -> ReviewReport:
    with closing(get_conn()) as conn:
        return load_review_report(conn, review_run_id)


def _begin_native_publication(
    review_run_id: int,
    provider: str,
) -> PublicationHandle:
    with closing(get_conn()) as conn, conn:
        return begin_publication(
            conn,
            review_run_id,
            scm_provider=provider,
        )


def _mark_native_publication_published(
    publication_id: int,
    review_run_id: int,
    provider: str,
    published: PublishedReview,
) -> None:
    with closing(get_conn()) as conn, conn:
        record_finding_threads(
            conn,
            review_run_id=review_run_id,
            scm_provider=provider,
            comments=published.finding_comments,
        )
        mark_publication_published(
            conn,
            publication_id,
            external_id=published.external_id,
            external_url=published.external_url,
        )


def _mark_native_publication_failed(publication_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_publication_failed(conn, publication_id)


def _begin_native_auto_approval(
    review_run_id: int,
    event: PullRequestEvent,
    policy_fingerprint: str,
    decision: AutoApprovalDecision,
) -> AutoApprovalHandle:
    with closing(get_conn()) as conn, conn:
        return begin_auto_approval(
            conn,
            review_run_id=review_run_id,
            scm_provider=event.provider,
            head_sha=event.head_sha,
            policy_fingerprint=policy_fingerprint,
            decision=decision,
        )


def _mark_native_auto_approval_published(
    approval_id: int,
    published: PublishedApproval,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_auto_approval_published(
            conn,
            approval_id,
            external_id=published.external_id,
            external_url=published.external_url,
        )


def _mark_native_auto_approval_failed(approval_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_auto_approval_failed(conn, approval_id)


def _mark_native_auto_approval_cancelled(
    approval_id: int,
    error_code: str,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_auto_approval_cancelled(
            conn,
            approval_id,
            error_code=error_code,
        )


def _mark_native_review_superseded(review_run_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_review_superseded(conn, review_run_id)


def _mark_native_review_terminal_failed(workflow_job_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_review_terminal_failed(conn, workflow_job_id)


def _latest_native_review_head(
    pull_request_id: int,
    review_run_id: int,
) -> str | None:
    with closing(get_conn()) as conn:
        return latest_published_review_head(
            conn,
            pull_request_id=pull_request_id,
            before_review_run_id=review_run_id,
        )


def _load_native_continuity(review_run_id: int) -> ReviewContinuity:
    with closing(get_conn()) as conn:
        return load_review_continuity(conn, review_run_id)


def _begin_native_thread_operations(
    review_run_id: int,
) -> tuple[ThreadOperationHandle, ...]:
    with closing(get_conn()) as conn, conn:
        return begin_thread_operations(conn, review_run_id=review_run_id)


def _mark_native_thread_operation_published(
    operation_id: int,
    result: PublishedThreadOperation,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_thread_operation_published(
            conn,
            operation_id,
            result=result,
        )


def _mark_native_thread_operation_failed(operation_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_thread_operation_failed(conn, operation_id)


def _begin_conversation(workflow_job_id: int) -> ConversationWork:
    with closing(get_conn()) as conn, conn:
        return begin_conversation_generation(
            conn,
            workflow_job_id=workflow_job_id,
        )


def _mark_conversation_ready(
    conversation_id: int,
    *,
    answer,
    snapshot_id: int | None,
    context_chunk_count: int,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_conversation_ready(
            conn,
            conversation_id,
            answer=answer.answer,
            references=answer.references,
            index_snapshot_id=snapshot_id,
            model=conversation_model(),
            prompt_version=CONVERSATION_PROMPT_VERSION,
            context_chunk_count=context_chunk_count,
            prompt_tokens=answer.prompt_tokens,
            completion_tokens=answer.completion_tokens,
        )


def _ignore_conversation(conversation_id: int, reason_code: str) -> None:
    with closing(get_conn()) as conn, conn:
        mark_conversation_ignored(
            conn,
            conversation_id,
            reason_code=reason_code,
        )


def _begin_conversation_publication(
    conversation_id: int,
) -> ConversationPublication:
    with closing(get_conn()) as conn, conn:
        return begin_conversation_publication(conn, conversation_id)


def _mark_conversation_published(conversation_id: int, result) -> None:
    with closing(get_conn()) as conn, conn:
        mark_conversation_published(
            conn,
            conversation_id,
            result=result,
        )


def _mark_conversation_failed(workflow_job_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_conversation_failed(
            conn,
            workflow_job_id=workflow_job_id,
        )


def _begin_feedback_sync(
    workflow_job_id: int,
    event: FeedbackSyncEvent,
) -> FeedbackSyncTarget:
    with closing(get_conn()) as conn, conn:
        return begin_feedback_sync(
            conn,
            workflow_job_id=workflow_job_id,
            event=event,
        )


def _reconcile_feedback_reactions(target, reactions):
    with closing(get_conn()) as conn, conn:
        return reconcile_review_reactions(conn, target, reactions)


def _mark_feedback_sync_failed(workflow_job_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_feedback_sync_failed(
            conn,
            workflow_job_id=workflow_job_id,
        )


def _begin_rule_learning_job(
    workflow_job_id: int,
    event: RuleLearningJobEvent,
) -> RuleLearningWork:
    with closing(get_conn()) as conn, conn:
        return begin_rule_learning(
            conn,
            workflow_job_id=workflow_job_id,
            event=event,
            model=rule_learning_model(),
            prompt_version=RULE_LEARNING_PROMPT_VERSION,
        )


def _mark_rule_learning_job_failed(workflow_job_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_rule_learning_failed(
            conn,
            workflow_job_id=workflow_job_id,
        )


def _generate_and_persist_rule_learning(
    job: WorkflowJob,
    work: RuleLearningWork,
    worker_id: str,
) -> object:
    minimum_support = int(os.environ.get("SUGGESTED_RULE_MIN_SUPPORT", "3"))
    minimum_support_pull_requests = int(
        os.environ.get("SUGGESTED_RULE_MIN_SUPPORT_PULL_REQUESTS", "3")
    )

    def report_progress() -> None:
        if not _heartbeat_lease(job.id, worker_id):
            raise RuntimeError("Workflow lease was lost during suggested-rule generation")

    suggestions, prompt_tokens, completion_tokens = generate_suggested_rules(
        work.evidence,
        minimum_support=minimum_support,
        minimum_support_pull_requests=minimum_support_pull_requests,
        progress_callback=report_progress,
    )
    with closing(get_conn()) as conn, conn:
        return persist_rule_suggestions(
            conn,
            work=work,
            suggestions=suggestions,
            minimum_support=minimum_support,
            minimum_support_pull_requests=minimum_support_pull_requests,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _generate_and_persist_conversation(
    job: WorkflowJob,
    event: ReviewConversationEvent,
    work: ConversationWork,
    contexts: list,
    snapshot_id: int | None,
    worker_id: str,
) -> None:
    def report_progress() -> None:
        if not _heartbeat_lease(job.id, worker_id):
            raise RuntimeError("Workflow lease was lost while answering a review question")

    answer = generate_conversation_answer(
        event,
        work.finding,
        contexts,
        work.previous_turns,
        progress_callback=report_progress,
    )
    report_progress()
    _mark_conversation_ready(
        work.id,
        answer=answer,
        snapshot_id=snapshot_id,
        context_chunk_count=len(contexts),
    )


async def _publish_native_thread_operations(
    event: PullRequestEvent,
    review_run_id: int,
) -> None:
    operations = await anyio.to_thread.run_sync(
        partial(_begin_native_thread_operations, review_run_id)
    )
    for operation in operations:
        try:
            if event.provider == "github":
                result = await apply_github_thread_operation(event, operation)
            elif event.provider == "gitlab":
                result = await apply_gitlab_thread_operation(event, operation)
            else:
                raise ValueError(
                    f"Unsupported SCM provider: {event.provider}"
                )
        except Exception:
            await anyio.to_thread.run_sync(
                partial(
                    _mark_native_thread_operation_failed,
                    operation.id,
                )
            )
            raise
        await anyio.to_thread.run_sync(
            partial(
                _mark_native_thread_operation_published,
                operation.id,
                result,
            )
        )


def _begin_native_check(
    review_run_id: int,
    event: PullRequestEvent,
) -> CheckRunHandle:
    with closing(get_conn()) as conn, conn:
        return begin_check_run(
            conn,
            review_run_id=review_run_id,
            scm_provider=event.provider,
            head_sha=event.head_sha,
        )


def _get_native_check_for_job(workflow_job_id: int) -> CheckRunHandle | None:
    with closing(get_conn()) as conn:
        return get_check_run_for_workflow_job(conn, workflow_job_id)


def _mark_native_check_started(
    check_run_id: int,
    external_id: str,
    external_url: str | None,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_check_run_started(
            conn,
            check_run_id,
            external_id=external_id,
            external_url=external_url,
        )


def _mark_native_check_completing(check_run_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_check_run_completing(conn, check_run_id)


def _mark_native_check_completed(check_run_id: int, conclusion: str) -> None:
    with closing(get_conn()) as conn, conn:
        mark_check_run_completed(
            conn,
            check_run_id,
            conclusion=conclusion,
        )


def _mark_native_check_failed(check_run_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        mark_check_run_failed(conn, check_run_id)


async def _ensure_native_check(
    event: PullRequestEvent,
    review_run_id: int,
) -> CheckRunHandle:
    handle = await anyio.to_thread.run_sync(
        partial(_begin_native_check, review_run_id, event)
    )
    if handle.is_completed:
        return handle
    try:
        if event.provider == "github":
            published = await ensure_github_check_run(
                event,
                external_key=handle.external_key,
                existing_external_id=handle.external_id,
                existing_external_url=handle.external_url,
            )
        elif event.provider == "gitlab":
            published = await ensure_gitlab_check_run(
                event,
                external_key=handle.external_key,
                existing_external_id=handle.external_id,
                existing_external_url=handle.external_url,
            )
        else:
            raise ValueError(f"Unsupported SCM provider: {event.provider}")
        await anyio.to_thread.run_sync(
            partial(
                _mark_native_check_started,
                handle.id,
                published.external_id,
                published.external_url,
            )
        )
    except Exception:
        await anyio.to_thread.run_sync(
            partial(_mark_native_check_failed, handle.id)
        )
        raise
    return CheckRunHandle(
        id=handle.id,
        status="in_progress",
        external_key=handle.external_key,
        external_id=published.external_id,
        external_url=published.external_url,
        conclusion=None,
    )


async def _complete_native_check(
    event: PullRequestEvent,
    handle: CheckRunHandle | None,
    *,
    conclusion: str,
    blocking_severities: tuple[str, ...] = ("critical", "high"),
    report: ReviewReport | None = None,
    message: str | None = None,
    unresolved_findings: tuple[ReviewFinding, ...] | None = None,
) -> None:
    if handle is None or handle.is_completed:
        return
    if handle.external_id is None:
        await anyio.to_thread.run_sync(
            partial(_mark_native_check_failed, handle.id)
        )
        return
    await anyio.to_thread.run_sync(
        partial(_mark_native_check_completing, handle.id)
    )
    try:
        if event.provider == "github":
            await complete_github_check_run(
                event,
                external_id=handle.external_id,
                conclusion=conclusion,
                blocking_severities=blocking_severities,
                report=report,
                message=message,
                unresolved_findings=unresolved_findings,
            )
        elif event.provider == "gitlab":
            await complete_gitlab_check_run(
                event,
                external_id=handle.external_id,
                conclusion=conclusion,
                blocking_severities=blocking_severities,
                report=report,
                message=message,
                unresolved_findings=unresolved_findings,
            )
        else:
            raise ValueError(f"Unsupported SCM provider: {event.provider}")
    except Exception:
        await anyio.to_thread.run_sync(
            partial(_mark_native_check_failed, handle.id)
        )
        raise
    await anyio.to_thread.run_sync(
        partial(_mark_native_check_completed, handle.id, conclusion)
    )


async def _publish_native_auto_approval(
    event: PullRequestEvent,
    *,
    review_run_id: int,
    decision: AutoApprovalDecision,
) -> PublishedApproval:
    if event.provider == "github":
        publisher = publish_github_approval
    elif event.provider == "gitlab":
        publisher = publish_gitlab_approval
    else:
        raise ValueError(f"Unsupported SCM provider: {event.provider}")
    return await publisher(
        event,
        review_run_id=review_run_id,
        decision=decision,
    )


async def _complete_existing_job_check(
    job: WorkflowJob,
    event: PullRequestEvent,
    *,
    conclusion: str,
    message: str,
) -> None:
    handle = await anyio.to_thread.run_sync(
        partial(_get_native_check_for_job, job.id)
    )
    await _complete_native_check(
        event,
        handle,
        conclusion=conclusion,
        message=message,
    )


def _index_repository_job(job: WorkflowJob, event: PushEvent, worker_id: str) -> None:
    with closing(get_conn()) as conn:
        repository = get_repository(conn, job.repository_id)
    if repository is None or not repository.enabled:
        raise ValueError("Repository is disabled or is not configured for mirroring")
    if (
        repository.scm_provider != event.provider
        or repository.scm_base_url != event.scm_base_url
        or repository.full_name != event.repo_full_name
        or repository.default_branch != event.default_branch
        or event.after_sha != job.revision
    ):
        raise ValueError("Index workflow identity does not match repository configuration")

    def report_progress() -> None:
        if not _heartbeat_and_check_latest(job.id, worker_id):
            raise IndexSupersededError("A newer index request or worker lease replaced this job")

    _set_mirror_state(repository.id, state="syncing")
    try:
        mirror = RepositoryMirror(repository)
        with mirror.checkout(event.after_sha) as worktree:
            report_progress()
            index_repo(
                str(worktree),
                repository.full_name,
                scm_provider=repository.scm_provider,
                scm_base_url=repository.scm_base_url,
                default_branch=repository.default_branch,
                progress_callback=report_progress,
            )
        _set_mirror_state(
            repository.id,
            state="ready",
            commit_sha=event.after_sha,
        )
    except IndexSupersededError:
        raise
    except RepositoryMirrorError:
        _set_mirror_state(
            repository.id,
            state="failed",
            error_code="mirror_sync_failed",
        )
        raise
    except Exception:
        _set_mirror_state(
            repository.id,
            state="failed",
            error_code="indexing_failed",
        )
        raise


async def process_review_job(job: WorkflowJob, worker_id: str) -> None:
    if job.job_type != "review_pull_request":
        raise ValueError(f"Unsupported workflow job type: {job.job_type}")
    event = PullRequestEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise ValueError("Workflow job identity does not match its payload")

    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_current, job.id, worker_id)):
        await _complete_existing_job_check(
            job,
            event,
            conclusion="cancelled",
            message="A newer pull-request event superseded this review.",
        )
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    diff_text = await _fetch_scm_pull_request_diff(event)
    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_current, job.id, worker_id)):
        await _complete_existing_job_check(
            job,
            event,
            conclusion="cancelled",
            message="A newer pull-request event superseded this review.",
        )
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return

    snapshot_id = await anyio.to_thread.run_sync(
        partial(compatible_snapshot_id, event.repo_full_name, job.repository_id)
    )
    policy = await anyio.to_thread.run_sync(
        partial(_load_review_policy, snapshot_id, diff_text, job.repository_id)
    )
    context_plan = await anyio.to_thread.run_sync(
        partial(
            _load_cross_repository_context_plan,
            job.repository_id,
            snapshot_id,
            policy,
        )
    )
    decision = _trigger_decision(event, diff_text, policy)
    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_current, job.id, worker_id)):
        await _complete_existing_job_check(
            job,
            event,
            conclusion="cancelled",
            message="A newer pull-request event superseded this review.",
        )
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return

    review_run = await anyio.to_thread.run_sync(
        partial(
            _begin_native_review,
            job,
            event,
            context_plan,
            policy,
        )
    )
    if review_run.status == "superseded":
        await _complete_existing_job_check(
            job,
            event,
            conclusion="cancelled",
            message="A newer pull-request event superseded this review.",
        )
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    if review_run.needs_generation and not decision.eligible:
        await anyio.to_thread.run_sync(
            partial(
                _persist_trigger_skip,
                review_run.id,
                diff_text,
                policy,
                decision,
            )
        )

    check_run = None
    if decision.eligible and policy.triggers.status_check:
        check_run = await _ensure_native_check(event, review_run.id)
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_and_check_current, job.id, worker_id)
        ):
            await _complete_native_check(
                event,
                check_run,
                conclusion="cancelled",
                message="A newer pull-request event superseded this review.",
            )
            await anyio.to_thread.run_sync(
                partial(_mark_native_review_superseded, review_run.id)
            )
            await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
            return

    touched_paths: frozenset[str] = frozenset()
    if decision.eligible and review_run.needs_generation:
        if job.pull_request_id is None:
            raise ValueError("Review job does not reference a pull request")
        previous_head = await anyio.to_thread.run_sync(
            partial(
                _latest_native_review_head,
                job.pull_request_id,
                review_run.id,
            )
        )
        if previous_head and previous_head != event.head_sha:
            update_diff = await _fetch_scm_pull_request_update_diff(
                event,
                previous_head,
            )
            touched_paths = frozenset(
                validate_repo_path(path)
                for file in parse_unified_diff(update_diff).files
                for path in (file.old_path, file.new_path)
                if path is not None
            )

    context_bundle = None
    if decision.eligible and review_run.needs_generation:
        filtered_diff = _policy_filtered_diff(diff_text, policy)
        try:
            context_bundle = await anyio.to_thread.run_sync(
                partial(
                    retrieve_context_from_plan,
                    filtered_diff,
                    context_plan,
                )
            )
        except Exception:
            if check_run is not None:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_check_failed, check_run.id)
                )
            raise
    if review_run.needs_generation and decision.eligible:
        try:
            await anyio.to_thread.run_sync(
                partial(
                    _generate_and_persist_review,
                    job,
                    review_run.id,
                    diff_text,
                    list(context_bundle.contexts if context_bundle else ()),
                    worker_id,
                    policy,
                    touched_paths,
                )
            )
        except ReviewSupersededError:
            await _complete_native_check(
                event,
                check_run,
                conclusion="cancelled",
                message="A newer pull-request event superseded this review.",
            )
            await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
            return
        except Exception:
            if check_run is not None:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_check_failed, check_run.id)
                )
            raise
    try:
        report = await anyio.to_thread.run_sync(
            partial(_load_native_report, review_run.id)
        )
    except Exception:
        if check_run is not None:
            await anyio.to_thread.run_sync(
                partial(_mark_native_check_failed, check_run.id)
            )
        raise
    continuity = (
        await anyio.to_thread.run_sync(
            partial(_load_native_continuity, review_run.id)
        )
        if report.publication_enabled
        else ReviewContinuity()
    )
    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_current, job.id, worker_id)):
        await anyio.to_thread.run_sync(partial(_mark_native_review_superseded, review_run.id))
        await _complete_native_check(
            event,
            check_run,
            conclusion="cancelled",
            message="A newer pull-request event superseded this review.",
        )
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return

    if report.publication_enabled:
        publication = await anyio.to_thread.run_sync(
            partial(_begin_native_publication, review_run.id, event.provider)
        )
        if publication.status != "published":
            try:
                if event.provider == "github":
                    published = await publish_github_review(
                        event,
                        review_run_id=review_run.id,
                        report=report,
                        review_number=publication.review_number,
                        continuity=continuity,
                    )
                elif event.provider == "gitlab":
                    published = await publish_gitlab_review(
                        event,
                        review_run_id=review_run.id,
                        report=report,
                        diff_text=diff_text,
                        review_number=publication.review_number,
                        continuity=continuity,
                    )
                else:
                    raise ValueError(
                        f"Unsupported SCM provider: {event.provider}"
                    )
            except Exception:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_publication_failed, publication.id)
                )
                if check_run is not None:
                    await anyio.to_thread.run_sync(
                        partial(_mark_native_check_failed, check_run.id)
                    )
                raise
            await anyio.to_thread.run_sync(
                partial(
                    _mark_native_publication_published,
                    publication.id,
                    review_run.id,
                    event.provider,
                    published,
                )
            )
        try:
            await _publish_native_thread_operations(event, review_run.id)
        except Exception:
            if check_run is not None:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_check_failed, check_run.id)
                )
            raise
    if check_run is not None:
        conclusion = review_check_conclusion(
            report,
            policy.triggers.blocking_severities,
            unresolved_findings=continuity.open_findings,
        )
        await _complete_native_check(
            event,
            check_run,
            conclusion=conclusion,
            blocking_severities=policy.triggers.blocking_severities,
            report=report,
            unresolved_findings=continuity.open_findings,
        )
    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_current, job.id, worker_id)):
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    if report.publication_enabled and policy.auto_approval_requested:
        approval_decision = evaluate_auto_approval(
            policy,
            event,
            diff_text,
            report,
            unresolved_findings=continuity.open_findings,
        )
        approval = await anyio.to_thread.run_sync(
            partial(
                _begin_native_auto_approval,
                review_run.id,
                event,
                policy.fingerprint,
                approval_decision,
            )
        )
        if not approval.is_terminal:
            try:
                published_approval = await _publish_native_auto_approval(
                    event,
                    review_run_id=review_run.id,
                    decision=approval_decision,
                )
            except ApprovalNotCurrentError as error:
                await anyio.to_thread.run_sync(
                    partial(
                        _mark_native_auto_approval_cancelled,
                        approval.id,
                        error.code,
                    )
                )
            except Exception:
                await anyio.to_thread.run_sync(
                    partial(
                        _mark_native_auto_approval_failed,
                        approval.id,
                    )
                )
                raise
            else:
                await anyio.to_thread.run_sync(
                    partial(
                        _mark_native_auto_approval_published,
                        approval.id,
                        published_approval,
                    )
                )
    completed = await anyio.to_thread.run_sync(partial(_complete, job.id, worker_id))
    if not completed:
        raise RuntimeError("Workflow lease was lost before completion")
    LOGGER.info(
        "Review completed repo=%s pr=%s revision=%s context_chunks=%s job=%s",
        event.repo_full_name,
        event.number,
        event.head_sha,
        len(context_bundle.contexts) if context_bundle else 0,
        job.id,
    )


async def process_conversation_job(job: WorkflowJob, worker_id: str) -> None:
    if job.job_type != "answer_review_comment" or job.pull_request_id is None:
        raise ValueError(f"Unsupported conversation workflow job: {job.job_type}")
    event = ReviewConversationEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise ValueError("Conversation workflow identity does not match its payload")

    try:
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before conversation processing")
        work = await anyio.to_thread.run_sync(partial(_begin_conversation, job.id))
        if work.root_comment_id != event.root_comment_id:
            raise ValueError("Conversation thread does not match its workflow payload")
        if work.status == "ignored":
            completed = await anyio.to_thread.run_sync(
                partial(_complete, job.id, worker_id)
            )
            if not completed:
                raise RuntimeError("Workflow lease was lost before completion")
            return
        if work.is_published:
            completed = await anyio.to_thread.run_sync(
                partial(_complete, job.id, worker_id)
            )
            if not completed:
                raise RuntimeError("Workflow lease was lost before completion")
            return

        if work.needs_generation:
            retrieval_diff = build_conversation_retrieval_diff(event, work.finding)
            snapshot_id = await anyio.to_thread.run_sync(
                partial(compatible_snapshot_id, event.repo_full_name, job.repository_id)
            )
            policy = await anyio.to_thread.run_sync(
                partial(
                    _load_review_policy,
                    snapshot_id,
                    retrieval_diff,
                    job.repository_id,
                )
            )
            path_policy = policy.for_path(event.file_path)
            if (
                path_policy is None
                or not path_policy.reviewable
                or not path_policy.respond_to_comments
            ):
                await anyio.to_thread.run_sync(
                    partial(
                        _ignore_conversation,
                        work.id,
                        "conversation_disabled",
                    )
                )
                completed = await anyio.to_thread.run_sync(
                    partial(_complete, job.id, worker_id)
                )
                if not completed:
                    raise RuntimeError("Workflow lease was lost before completion")
                return
            context_bundle = await anyio.to_thread.run_sync(
                partial(
                    retrieve_context_from_snapshot,
                    event.repo_full_name,
                    retrieval_diff,
                    snapshot_id,
                )
            )
            await anyio.to_thread.run_sync(
                partial(
                    _generate_and_persist_conversation,
                    job,
                    event,
                    work,
                    list(context_bundle.contexts),
                    snapshot_id,
                    worker_id,
                )
            )

        publication = await anyio.to_thread.run_sync(
            partial(_begin_conversation_publication, work.id)
        )
        if not publication.is_published:
            if event.provider == "github":
                result = await publish_github_conversation_reply(
                    event,
                    answer=publication.answer,
                    references=publication.references,
                )
            elif event.provider == "gitlab":
                result = await publish_gitlab_conversation_reply(
                    event,
                    answer=publication.answer,
                    references=publication.references,
                )
            else:
                raise ValueError(
                    f"Unsupported SCM provider: {event.provider}"
                )
            await anyio.to_thread.run_sync(
                partial(_mark_conversation_published, work.id, result)
            )
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before conversation completion")
        completed = await anyio.to_thread.run_sync(
            partial(_complete, job.id, worker_id)
        )
        if not completed:
            raise RuntimeError("Workflow lease was lost before conversation completion")
        LOGGER.info(
            "Conversation answered repo=%s pr=%s thread=%s revision=%s job=%s",
            event.repo_full_name,
            event.number,
            event.root_comment_id,
            event.head_sha,
            job.id,
        )
    except Exception:
        await anyio.to_thread.run_sync(
            partial(_mark_conversation_failed, job.id)
        )
        raise


async def process_index_job(job: WorkflowJob, worker_id: str) -> None:
    event = PushEvent.from_payload(job.payload)
    if (
        job.pull_request_id is not None
        or event.after_sha != job.revision
        or event.after_sha != job.base_revision
        or event.scope_key != job.scope_key
    ):
        raise ValueError("Index workflow identity does not match its payload")

    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_latest, job.id, worker_id)):
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    try:
        await anyio.to_thread.run_sync(partial(_index_repository_job, job, event, worker_id))
    except IndexSupersededError:
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    if not await anyio.to_thread.run_sync(partial(_heartbeat_and_check_latest, job.id, worker_id)):
        await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
        return
    completed = await anyio.to_thread.run_sync(partial(_complete, job.id, worker_id))
    if not completed:
        raise RuntimeError("Workflow lease was lost before index completion")
    LOGGER.info(
        "Repository index completed repo=%s revision=%s job=%s",
        event.repo_full_name,
        event.after_sha,
        job.id,
    )


async def process_feedback_sync_job(job: WorkflowJob, worker_id: str) -> None:
    if job.job_type != "sync_review_feedback" or job.pull_request_id is None:
        raise ValueError(f"Unsupported feedback workflow job: {job.job_type}")
    event = FeedbackSyncEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise ValueError("Feedback workflow identity does not match its payload")

    try:
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before feedback processing")
        target = await anyio.to_thread.run_sync(
            partial(_begin_feedback_sync, job.id, event)
        )
        if event.provider == "github":
            reactions = await fetch_github_review_reactions(event)
        elif event.provider == "gitlab":
            reactions = await fetch_gitlab_review_reactions(event)
        else:
            raise ValueError(
                f"Unsupported SCM provider: {event.provider}"
            )
        result = await anyio.to_thread.run_sync(
            partial(_reconcile_feedback_reactions, target, reactions)
        )
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before feedback completion")
        completed = await anyio.to_thread.run_sync(
            partial(_complete, job.id, worker_id)
        )
        if not completed:
            raise RuntimeError("Workflow lease was lost before feedback completion")
        LOGGER.info(
            (
                "Review feedback synchronized repo=%s pr=%s thread=%s "
                "positive=%s negative=%s observed=%s withdrawn=%s job=%s"
            ),
            event.repo_full_name,
            event.number,
            event.root_comment_id,
            result.active_positive,
            result.active_negative,
            result.observed,
            result.withdrawn,
            job.id,
        )
    except Exception:
        await anyio.to_thread.run_sync(
            partial(_mark_feedback_sync_failed, job.id)
        )
        raise


async def process_rule_learning_job(job: WorkflowJob, worker_id: str) -> None:
    if job.job_type != "generate_suggested_rules" or job.pull_request_id is not None:
        raise ValueError(f"Unsupported rule-learning workflow job: {job.job_type}")
    event = RuleLearningJobEvent.from_payload(job.payload)
    if (
        event.repository_id != job.repository_id
        or event.evidence_fingerprint != job.base_revision
        or event.evidence_fingerprint != job.revision
        or event.scope_key != job.scope_key
    ):
        raise ValueError("Rule-learning workflow identity does not match its payload")

    try:
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before rule learning")
        work = await anyio.to_thread.run_sync(
            partial(_begin_rule_learning_job, job.id, event)
        )
        if work.needs_generation:
            result = await anyio.to_thread.run_sync(
                partial(
                    _generate_and_persist_rule_learning,
                    job,
                    work,
                    worker_id,
                )
            )
        else:
            result = None
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before rule-learning completion")
        completed = await anyio.to_thread.run_sync(
            partial(_complete, job.id, worker_id)
        )
        if not completed:
            raise RuntimeError("Workflow lease was lost before rule-learning completion")
        LOGGER.info(
            (
                "Suggested-rule learning completed repo=%s status=%s "
                "proposed=%s consolidated=%s rejected=%s job=%s"
            ),
            event.repo_full_name,
            work.status,
            result.proposed if result else 0,
            result.consolidated if result else 0,
            result.rejected_candidates if result else 0,
            job.id,
        )
    except Exception:
        await anyio.to_thread.run_sync(
            partial(_mark_rule_learning_job_failed, job.id)
        )
        raise


async def process_job(job: WorkflowJob, worker_id: str) -> None:
    if job.job_type == "review_pull_request":
        await process_review_job(job, worker_id)
        return
    if job.job_type == "answer_review_comment":
        await process_conversation_job(job, worker_id)
        return
    if job.job_type == "index_repository":
        await process_index_job(job, worker_id)
        return
    if job.job_type == "sync_review_feedback":
        await process_feedback_sync_job(job, worker_id)
        return
    if job.job_type == "generate_suggested_rules":
        await process_rule_learning_job(job, worker_id)
        return
    raise ValueError(f"Unsupported workflow job type: {job.job_type}")


async def run_once(worker_id: str) -> bool:
    job = await anyio.to_thread.run_sync(partial(_claim, worker_id))
    if job is None:
        return False
    try:
        await process_job(job, worker_id)
    except Exception as error:
        retryable = not isinstance(error, ValueError)
        next_status = await anyio.to_thread.run_sync(
            partial(_fail, job.id, worker_id, retryable=retryable)
        )
        if (
            job.job_type == "review_pull_request"
            and next_status in {"dead", "failed"}
        ):
            try:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_review_terminal_failed, job.id)
                )
            except Exception:
                LOGGER.exception(
                    "Failed to finalize terminal review lineage job=%s",
                    job.id,
                )
            try:
                event = PullRequestEvent.from_payload(job.payload)
                await _complete_existing_job_check(
                    job,
                    event,
                    conclusion="failure",
                    message=(
                        "Diffuse could not complete this review after exhausting "
                        "its retry policy."
                    ),
                )
            except Exception:
                LOGGER.exception(
                    "Failed to finalize terminal review check job=%s",
                    job.id,
                )
        LOGGER.exception(
            "Workflow job failed job=%s job_type=%s error_type=%s next_status=%s",
            job.id,
            job.job_type,
            type(error).__name__,
            next_status,
        )
    return True


async def run_forever(worker_id: str, poll_seconds: float) -> None:
    scheduler_seconds = float(
        os.environ.get("FEEDBACK_SYNC_SCHEDULER_SECONDS", "60")
    )
    if scheduler_seconds <= 0:
        raise ValueError("FEEDBACK_SYNC_SCHEDULER_SECONDS must be positive")
    learning_scheduler_seconds = float(
        os.environ.get("RULE_LEARNING_SCHEDULER_SECONDS", "300")
    )
    if learning_scheduler_seconds <= 0:
        raise ValueError("RULE_LEARNING_SCHEDULER_SECONDS must be positive")
    next_feedback_schedule = 0.0
    next_learning_schedule = 0.0
    while True:
        now = time.monotonic()
        if now >= next_feedback_schedule:
            try:
                scheduled = await anyio.to_thread.run_sync(
                    _schedule_feedback_syncs
                )
                if scheduled:
                    LOGGER.info(
                        "Scheduled review feedback synchronization jobs count=%s",
                        scheduled,
                    )
            except Exception:
                LOGGER.exception("Failed to schedule review feedback synchronization")
            next_feedback_schedule = now + scheduler_seconds
        if now >= next_learning_schedule:
            try:
                scheduled = await anyio.to_thread.run_sync(
                    _schedule_rule_learning
                )
                if scheduled:
                    LOGGER.info(
                        "Scheduled suggested-rule generation jobs count=%s",
                        scheduled,
                    )
            except Exception:
                LOGGER.exception("Failed to schedule suggested-rule generation")
            next_learning_schedule = now + learning_scheduler_seconds
        claimed = await run_once(worker_id)
        if not claimed:
            await anyio.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}",
    )
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    with closing(get_conn()) as conn:
        verify_database_current(conn)
    if args.once:
        anyio.run(run_once, args.worker_id)
    else:
        anyio.run(run_forever, args.worker_id, args.poll_seconds)


if __name__ == "__main__":
    main()
