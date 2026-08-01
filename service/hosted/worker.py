"""Durable Diffuse review worker."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import sys
import time
from contextlib import closing
from functools import partial

import anyio

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
    repository_failure_comment_enabled,
    resolve_review_policy,
)
from repository_policy.store import load_repository_policy
from retriever.context_models import CrossRepositoryContextPlan
from retriever.retrieve import (
    compatible_snapshot_id,
    max_context_chars,
    max_context_chunks,
    retrieve_context_from_plan,
    retrieve_context_from_snapshot,
)
from service.approval_publication import (
    ApprovalNotCurrentError,
    PublishedApproval,
)
from service.auto_approval import AutoApprovalDecision, evaluate_auto_approval
from service.conversation_engine import (
    CONVERSATION_PROMPT_VERSION,
    build_conversation_retrieval_diff,
    conversation_model,
    generate_conversation_answer,
)
from service.cross_repository import (
    record_dropped_context_repositories,
    resolve_cross_repository_context,
)
from service.diff_parser import parse_unified_diff
from service.finding_lineage import ReviewContinuity
from service.github.api import (
    fetch_pull_request_commits,
    fetch_pull_request_diff,
    fetch_pull_request_update_diff,
)
from service.github.app import validate_app_configuration
from service.github.approval import (
    publish_github_approval,
)
from service.github.check import (
    complete_github_check_run,
    ensure_github_check_run,
    review_check_conclusion,
)
from service.github.conversation import publish_github_conversation_reply
from service.github.feedback import fetch_github_review_reactions
from service.github.review import (
    PublishedReview,
    post_github_review_failure_notice,
    publish_github_review,
)
from service.github.threads import apply_github_thread_operation
from service.hosted.repository_mirror import (
    RepositoryMirror,
    RepositoryMirrorError,
    max_repository_bytes,
)
from service.hosted.workflow import (
    NonRetryableError,
    StrandedReviewJob,
    WorkflowJob,
    claim_stranded_review_jobs,
    claim_workflow_job,
    complete_workflow_job,
    fail_workflow_job,
    heartbeat_workflow_job,
    review_update_debounce_seconds,
    schedule_due_feedback_sync_jobs,
    supersede_workflow_job,
    workflow_job_is_current,
    workflow_job_is_latest,
    workflow_queue_depth,
)
from service.learning_engine import (
    RULE_LEARNING_PROMPT_VERSION,
    generate_suggested_rules,
    rule_learning_model,
)
from service.models.learning import RuleLearningJobEvent, RuleLearningWork
from service.models.review import ReviewFinding, ReviewReport
from service.repositories import get_repository, update_mirror_state
from service.review.engine import (
    PROMPT_VERSION,
    ReviewDepthSupport,
    _model_api_base,
    _positive_int,
    _supports_json_schema,
    generate_review,
    minimum_review_confidence,
    model_retries,
    resolve_review_depth_support,
    review_depth,
    review_effort,
    review_model,
    review_passes,
    review_provenance_minimum_confidence,
    review_verifier_model,
)
from service.review.failure_notice import (
    TerminalReviewFailure,
    terminal_review_failure,
)
from service.review.provenance import (
    PullRequestCommits,
    PullRequestProvenance,
    ReviewModelPlan,
    classify_pull_request_provenance,
    select_review_model_plan,
)
from service.scm import (
    FeedbackSyncEvent,
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
    normalize_base_url,
    scm_api_timeout_seconds,
)
from service.storage.approval import (
    AutoApprovalHandle,
    begin_auto_approval,
    mark_auto_approval_cancelled,
    mark_auto_approval_failed,
    mark_auto_approval_published,
)
from service.storage.check import (
    CheckRunHandle,
    begin_check_run,
    get_check_run_for_workflow_job,
    mark_check_run_completed,
    mark_check_run_completing,
    mark_check_run_failed,
    mark_check_run_started,
)
from service.storage.conversation import (
    ConversationPublication,
    ConversationWork,
    begin_conversation_generation,
    begin_conversation_publication,
    mark_conversation_failed,
    mark_conversation_ignored,
    mark_conversation_published,
    mark_conversation_ready,
)
from service.storage.custom_context import load_active_custom_contexts
from service.storage.feedback import (
    FeedbackSyncTarget,
    begin_feedback_sync,
    mark_feedback_sync_failed,
    reconcile_review_reactions,
)
from service.storage.finding import (
    PublishedThreadOperation,
    ThreadOperationHandle,
    begin_thread_operations,
    latest_published_review_head,
    load_review_continuity,
    mark_thread_operation_failed,
    mark_thread_operation_published,
    record_finding_threads,
)
from service.storage.learning import (
    begin_rule_learning,
    load_active_learned_rules,
    mark_rule_learning_failed,
    persist_rule_suggestions,
    schedule_due_rule_learning_jobs,
)
from service.storage.migrations import verify_database_current
from service.storage.review import (
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

LOGGER = logging.getLogger(__name__)

# How long a terminal review job is left alone before the reconciler finalizes
# it, and how many it finalizes per pass. Constants rather than settings: they
# only have to stay clear of `run_once`, which finalizes its own failures within
# seconds, so there is nothing here an operator would need to tune.
STRANDED_REVIEW_GRACE_SECONDS = 300
STRANDED_REVIEW_BATCH_SIZE = 20
STRANDED_REVIEW_INTERVAL_SECONDS = 300.0


class ReviewSupersededError(RuntimeError):
    pass


class IndexSupersededError(RuntimeError):
    pass


class MissingRepositoryIndexError(RuntimeError):
    """The repository has no index snapshot in the current index format.

    Deliberately a ``RuntimeError`` rather than a ``NonRetryableError``: the
    condition is transient. A pull request opened while the initial index job is
    still running resolves itself once that job commits a snapshot, so the review
    must be retried instead of failing permanently. Reviewing without an index
    would publish a commit-pinned review with no retrieval context and no
    ``.diffuse`` policy.
    """


async def _fetch_scm_pull_request_diff(event: PullRequestEvent) -> str:
    if event.provider == "github":
        return await fetch_pull_request_diff(event)
    raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")


async def _fetch_scm_pull_request_commits(
    event: PullRequestEvent,
) -> PullRequestCommits:
    if event.provider == "github":
        return await fetch_pull_request_commits(event)
    raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")


async def _resolve_review_provenance(
    event: PullRequestEvent,
) -> PullRequestProvenance:
    try:
        commits = await _fetch_scm_pull_request_commits(event)
    except Exception:
        # Provenance improves independence but is not a reason to suppress an
        # otherwise valid review. A metadata outage falls back to cross-review.
        LOGGER.warning(
            "Could not load pull-request commit metadata for provenance routing",
            exc_info=True,
        )
        return PullRequestProvenance.unavailable()
    return classify_pull_request_provenance(
        commits,
        pull_request_author=event.author,
    )


async def _fetch_scm_pull_request_update_diff(
    event: PullRequestEvent,
    previous_head_sha: str,
) -> str:
    if event.provider == "github":
        return await fetch_pull_request_update_diff(event, previous_head_sha)
    raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")


def _lease_seconds() -> int:
    value = int(os.environ.get("WORKFLOW_LEASE_SECONDS", "1800"))
    if value < 60:
        raise NonRetryableError("WORKFLOW_LEASE_SECONDS must be at least 60")
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
    with closing(get_conn()) as conn, conn:
        return schedule_due_feedback_sync_jobs(
            conn,
            api_base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            github_scm_base_url=os.environ.get(
                "GITHUB_WEB_URL",
                "https://github.com",
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


def _reconcile_stranded_reviews() -> tuple[StrandedReviewJob, ...]:
    """Finalize the review lineage of jobs that died outside `run_once`.

    Claiming and marking share one transaction so the review runs stay locked
    throughout: a newer job that adopts one of them via `begin_review_run` waits,
    then wins, instead of having its in-flight run marked failed underneath it.
    """
    with closing(get_conn()) as conn, conn:
        jobs = claim_stranded_review_jobs(
            conn,
            grace_seconds=STRANDED_REVIEW_GRACE_SECONDS,
            limit=STRANDED_REVIEW_BATCH_SIZE,
        )
        for job in jobs:
            mark_review_terminal_failed(
                conn,
                job.id,
                error_code=terminal_review_failure(
                    job.id,
                    retries_exhausted=job.retries_exhausted,
                ).error_code,
            )
        return jobs


async def _finalize_stranded_reviews() -> int:
    """Unblock pull requests whose required check outlived its review job.

    Deliberately quieter than `run_once`'s handler: the status check carries the
    explanation, and this pass cannot tell whether that handler already posted
    the terminal-failure comment for the same job, so it does not post a second.
    """
    jobs = await anyio.to_thread.run_sync(_reconcile_stranded_reviews)
    for stranded in jobs:
        failure = terminal_review_failure(
            stranded.id,
            retries_exhausted=stranded.retries_exhausted,
        )
        try:
            event = PullRequestEvent.from_payload(stranded.payload)
        except Exception:
            LOGGER.exception(
                "Failed to decode a stranded review payload job=%s",
                stranded.id,
            )
            continue
        LOGGER.error(
            "Reconciled a stranded review job=%s repo=%s number=%s head=%s error_code=%s",
            stranded.id,
            event.repo_full_name,
            event.number,
            event.head_sha,
            failure.error_code,
        )
        try:
            handle = await anyio.to_thread.run_sync(
                partial(_get_native_check_for_job, stranded.id)
            )
            await _complete_native_check(
                event,
                handle,
                conclusion="failure",
                message=failure.summary,
            )
        except Exception:
            LOGGER.exception(
                "Failed to finalize a stranded review check job=%s",
                stranded.id,
            )
    return len(jobs)


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


def report_review_depth_support(support: ReviewDepthSupport) -> None:
    """Emit one depth resolution where `LOG_LEVEL` cannot delete it.

    A depth that was honoured exactly is ordinary startup information and goes
    to the log. A depth that was *not* is the only evidence an operator gets
    that they are paying for a review shallower than the one they configured,
    and `logging.basicConfig(level=LOG_LEVEL)` can throw it away -- `LOG_LEVEL`
    is documented in both env files, and at `ERROR` the entire report vanished
    while the refusal still fired. So the unhonoured case is written straight to
    stderr, which is what `review_cli.report_review_depth` already does and for
    the same reason.
    """

    lines = support.report_lines()
    if not lines:
        return
    if support.fully_honored:
        for line in lines:
            LOGGER.info("%s", line)
        return
    for line in lines:
        print(line, file=sys.stderr, flush=True)


def _begin_native_review(
    job: WorkflowJob,
    event: PullRequestEvent,
    context_plan: CrossRepositoryContextPlan,
    policy: ResolvedReviewPolicy,
    provenance: PullRequestProvenance,
    model_plan: ReviewModelPlan,
    depth_support: ReviewDepthSupport,
) -> ReviewRunHandle:
    if job.pull_request_id is None:
        raise NonRetryableError("Review job does not reference a pull request")
    with closing(get_conn()) as conn, conn:
        return begin_review_run(
            conn,
            workflow_job_id=job.id,
            repository_id=job.repository_id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=context_plan.primary_snapshot_id,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model=model_plan.candidate_model,
            verifier_model=model_plan.verifier_model,
            provenance=provenance.to_dict(),
            model_routing_reason=model_plan.reason_code,
            review_depth_resolution=depth_support.summary(),
            prompt_version=PROMPT_VERSION,
            context_fingerprint=_review_context_fingerprint(
                context_plan,
                policy,
                event,
                provenance,
                model_plan,
                depth_support,
            ),
            learned_rules=policy.approved_learned_rules,
            custom_contexts=policy.approved_custom_contexts,
            context_snapshots=context_plan.related_snapshots,
        )


def _review_context_fingerprint(
    context_plan: CrossRepositoryContextPlan,
    policy: ResolvedReviewPolicy,
    event: PullRequestEvent,
    provenance: PullRequestProvenance,
    model_plan: ReviewModelPlan,
    depth_support: ReviewDepthSupport,
) -> str:
    """Everything that decides what a review would say, in one value.

    This is the identity of a review run. `begin_review_run` serves an existing
    run whose (pull request, base, head, model, prompt version, fingerprint)
    already matches, and a run that is already `ready` is republished without
    generating anything -- so an input left out here is an input an operator can
    change while still being served the previous review.

    Review depth was such an input. It is *recorded* on the run
    (`review_depth_resolution`), but recording is not identity: raising
    `REVIEW_DEPTH` and re-running found the shallower run ready and republished
    it, which is the same silent no-op the depth work exists to delete. The
    resolved summary is used rather than the bare variable because it names both
    what was asked and what each stage will actually be sent, so a depth the
    route steps down or refuses is distinguished from one it honours.

    `summary()` is `None` exactly when no depth was requested, and contributes
    nothing at all there -- not an empty component, which would still change the
    hash -- so an installation that never set a depth keeps the fingerprints its
    runs are already stored under and does not re-review every open pull request
    on upgrade.
    """

    components = [
        context_plan.fingerprint,
        policy.fingerprint,
        event.trigger_fingerprint,
        provenance.fingerprint,
        model_plan.fingerprint,
    ]
    depth_resolution = depth_support.summary()
    if depth_resolution is not None:
        components.append(depth_resolution)
    return hashlib.sha256("\0".join(components).encode()).hexdigest()


def _load_cross_repository_context_plan(
    repository_id: int,
    snapshot_id: int | None,
    policy: ResolvedReviewPolicy,
    worker_id: str,
) -> CrossRepositoryContextPlan:
    with closing(get_conn()) as conn, conn:
        resolution = resolve_cross_repository_context(
            conn,
            primary_repository_id=repository_id,
            primary_snapshot_id=snapshot_id,
            explicit_repositories=policy.context_repositories,
        )
        record_dropped_context_repositories(
            conn,
            primary_repository_id=repository_id,
            actor_label=worker_id,
            dropped=resolution.dropped_repositories,
        )
    for item in resolution.dropped_repositories:
        LOGGER.warning(
            "Dropped an unauthorized cross-repository context entry "
            "repository_id=%s context_repository=%s reason=%s",
            repository_id,
            item.repository_full_name,
            item.reason_code,
        )
    return resolution.plan


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


def _continuity_paths(
    update_diff: str,
) -> tuple[frozenset[str], dict[str, str]]:
    """Derive touched paths and rename aliases for finding continuity."""
    aliases: dict[str, str] = {}
    touched: set[str] = set()
    for file in parse_unified_diff(update_diff).files:
        old_path = validate_repo_path(file.old_path) if file.old_path else None
        new_path = validate_repo_path(file.new_path) if file.new_path else None
        if old_path and new_path and old_path != new_path:
            aliases[old_path] = new_path
            # Renames keep continuity via aliases; only the destination path is
            # treated as touched so the old path is not falsely addressed.
            touched.add(new_path)
        elif new_path:
            touched.add(new_path)
        elif old_path:
            touched.add(old_path)
    return frozenset(touched), aliases


def _generate_and_persist_review(
    job: WorkflowJob,
    review_run_id: int,
    diff_text: str,
    contexts: list,
    worker_id: str,
    policy: ResolvedReviewPolicy,
    touched_paths: frozenset[str],
    path_aliases: dict[str, str] | None = None,
    model_plan: ReviewModelPlan | None = None,
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
            candidate_model=(
                model_plan.candidate_model if model_plan is not None else None
            ),
            verifier_model=(
                model_plan.verifier_model if model_plan is not None else None
            ),
        )
        report_progress()
        with closing(get_conn()) as conn, conn:
            persist_review_report(
                conn,
                review_run_id,
                report,
                touched_paths=touched_paths,
                path_aliases=path_aliases,
            )
    except ReviewSupersededError:
        with closing(get_conn()) as conn, conn:
            if not mark_review_superseded(conn, review_run_id, worker_id=worker_id):
                # The lease is already gone, so another worker owns this review run
                # now. Superseding it here would delete the findings that worker is
                # generating; let it finish instead.
                LOGGER.warning(
                    "Not superseding review run %s: lease no longer held by %s",
                    review_run_id,
                    worker_id,
                )
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
            unanchored_fingerprints=frozenset(published.unattached_fingerprints),
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


def _mark_native_review_superseded(review_run_id: int, worker_id: str) -> bool:
    with closing(get_conn()) as conn, conn:
        return mark_review_superseded(conn, review_run_id, worker_id=worker_id)


def _mark_native_review_terminal_failed(
    workflow_job_id: int,
    error_code: str,
) -> None:
    with closing(get_conn()) as conn, conn:
        mark_review_terminal_failed(conn, workflow_job_id, error_code=error_code)


# Only a deliberate policy exclusion suppresses the notice. Notably absent:
# `metadata_unavailable`. That means Diffuse could not enrich the event from the
# provider API -- a common cause of the very failure being reported -- so
# treating it as "not eligible" would silence the notice in exactly the case it
# exists for. Anything unrecognized also falls through to posting.
_NOTICE_SUPPRESSING_REASONS = frozenset(
    {
        "automatic_disabled",
        "draft_pull_request",
        "updates_disabled",
        "disabled_label",
        "excluded_author",
        "excluded_branch",
        "excluded_keyword",
        "author_not_included",
        "branch_not_included",
        "required_label_missing",
        "required_keyword_missing",
        "file_change_limit",
    }
)


def _failure_notice_enabled(
    repo_full_name: str,
    repository_id: int | None,
    event: PullRequestEvent | None = None,
) -> bool:
    """Resolve whether this repository wants a terminal-failure notice here.

    Fails open: the whole point of the notice is that the default experience is
    not silence, so a policy that cannot be read still gets a notice.

    When the event is available this also declines to comment on a pull request
    Diffuse would never have reviewed. A terminal failure can happen before
    trigger evaluation, so without this check a draft, an excluded author, or a
    ``do not review`` pull request would be told that a review it never asked
    for did not complete.
    """
    try:
        snapshot_id = compatible_snapshot_id(repo_full_name, repository_id)
        if snapshot_id is None:
            return True
        with closing(get_conn()) as conn:
            snapshot = load_repository_policy(conn, snapshot_id)
    except Exception:
        LOGGER.exception(
            "Failed to resolve the review failure-notice policy repo=%s",
            repo_full_name,
        )
        return True
    if not repository_failure_comment_enabled(snapshot):
        return False
    if event is None:
        return True
    try:
        policy = resolve_review_policy(
            snapshot,
            (),
            default_passes=review_passes(),
            default_minimum_confidence=minimum_review_confidence(),
        )
        decision = _trigger_decision(event, "", policy)
    except (OSError, RuntimeError, ValueError):
        # Narrow deliberately. A broad `except Exception` here hid an
        # AttributeError on the decision field and made this gate silently
        # fail open while its tests still passed.
        LOGGER.exception(
            "Failed to evaluate failure-notice eligibility repo=%s number=%s",
            repo_full_name,
            event.number,
        )
        return True
    if decision.reason_code in _NOTICE_SUPPRESSING_REASONS:
        LOGGER.info(
            "Suppressed a terminal failure notice for an excluded pull request "
            "repo=%s number=%s reason=%s",
            repo_full_name,
            event.number,
            decision.reason_code,
        )
        return False
    return True


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
    job: WorkflowJob,
    worker_id: str,
) -> None:
    operations = await anyio.to_thread.run_sync(
        partial(_begin_native_thread_operations, review_run_id)
    )
    for operation in operations:
        # One provider round trip per unresolved finding, so the lease has to be
        # renewed inside the loop rather than only around it.
        await _extend_publication_lease(job, worker_id)
        try:
            if event.provider == "github":
                result = await apply_github_thread_operation(event, operation)
            else:
                raise NonRetryableError(
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
        else:
            raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")
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
        else:
            raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")
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
    else:
        raise NonRetryableError(f"Unsupported SCM provider: {event.provider}")
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


async def _extend_publication_lease(job: WorkflowJob, worker_id: str) -> None:
    """Renew the lease between publication steps.

    Publication is a chain of provider round trips -- a review body, then one
    thread operation per finding, then the status check -- with no heartbeat of
    its own, so a large pull request can outlive the lease part-way through and
    have its job swept out from under it. A lost lease is only logged: the sweep
    has already requeued the job, and abandoning a half-published review would
    leave the pull request worse off than finishing it.
    """
    if not await anyio.to_thread.run_sync(partial(_heartbeat_lease, job.id, worker_id)):
        LOGGER.warning(
            "Workflow lease was lost during review publication job=%s",
            job.id,
        )


async def _post_terminal_failure_notice(
    job: WorkflowJob,
    event: PullRequestEvent,
    failure: TerminalReviewFailure,
) -> None:
    """Tell the pull request that Diffuse gave up on it.

    Best effort by construction: the review has already failed, so every error
    here is logged and swallowed rather than allowed to mask the original
    failure or to queue more work.
    """
    try:
        enabled = await anyio.to_thread.run_sync(
            partial(
                _failure_notice_enabled,
                event.repo_full_name,
                job.repository_id,
                event,
            )
        )
        if not enabled:
            return
        if event.provider == "github":
            await post_github_review_failure_notice(event, failure=failure)
        else:
            LOGGER.error(
                "Cannot report a terminal review failure on provider=%s job=%s",
                event.provider,
                job.id,
            )
    except Exception:
        LOGGER.exception(
            "Failed to post the terminal review failure notice "
            "job=%s provider=%s repo=%s number=%s",
            job.id,
            event.provider,
            event.repo_full_name,
            event.number,
        )


def _index_repository_job(job: WorkflowJob, event: PushEvent, worker_id: str) -> None:
    with closing(get_conn()) as conn:
        repository = get_repository(conn, job.repository_id)
    if repository is None or not repository.enabled:
        raise NonRetryableError("Repository is disabled or is not configured for mirroring")
    if (
        repository.scm_provider != event.provider
        or repository.scm_base_url != event.scm_base_url
        or repository.full_name != event.repo_full_name
        or repository.default_branch != event.default_branch
        or event.after_sha != job.revision
    ):
        raise NonRetryableError("Index workflow identity does not match repository configuration")

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
        raise NonRetryableError(f"Unsupported workflow job type: {job.job_type}")
    event = PullRequestEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise NonRetryableError("Workflow job identity does not match its payload")

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
    if snapshot_id is None:
        raise MissingRepositoryIndexError(
            "Repository has no compatible active index; run repository sync first"
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
            worker_id,
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

    provenance = PullRequestProvenance.not_evaluated()
    if decision.eligible:
        provenance = await _resolve_review_provenance(event)
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_and_check_current, job.id, worker_id)
        ):
            await _complete_existing_job_check(
                job,
                event,
                conclusion="cancelled",
                message="A newer pull-request event superseded this review.",
            )
            await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
            return
    model_plan = select_review_model_plan(
        provenance,
        candidate_model=review_model(),
        verifier_model=review_verifier_model(),
        minimum_confidence=review_provenance_minimum_confidence(),
    )
    # Startup validated the *configured* pair. Routing permutes it, so on an
    # AI-authored pull request the candidate pass runs on the model configured
    # as the verifier -- a pair no validator has looked at, and one that can
    # have no reasoning control at all while the configured candidate had one.
    # Resolve the pair that will actually be used, and record it on the run:
    # refusing here would dead-letter the pull request over configuration the
    # operator can only change between runs.
    depth_support = resolve_review_depth_support(
        candidate_model=model_plan.candidate_model,
        verifier_model=model_plan.verifier_model,
        source=f"routed by provenance: {model_plan.reason_code}",
    )
    report_review_depth_support(depth_support)

    review_run = await anyio.to_thread.run_sync(
        partial(
            _begin_native_review,
            job,
            event,
            context_plan,
            policy,
            provenance,
            model_plan,
            depth_support,
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

    # Status checks follow the review-run head, including intentional skips.
    # Gating creation on eligibility left branch protection hanging forever when
    # synchronize / draft / label filters produced a skipped report (DEV-306).
    check_run = None
    if policy.triggers.status_check:
        check_run = await _ensure_native_check(event, review_run.id)
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_and_check_current, job.id, worker_id)
        ):
            # Retire the run first: losing the lease is indistinguishable here from a
            # genuine supersession, and only the store can tell them apart atomically.
            # A worker that no longer owns the run must not cancel the check either --
            # the worker that does own it is still going to complete it.
            if await anyio.to_thread.run_sync(
                partial(_mark_native_review_superseded, review_run.id, worker_id)
            ):
                await _complete_native_check(
                    event,
                    check_run,
                    conclusion="cancelled",
                    message="A newer pull-request event superseded this review.",
                )
            await anyio.to_thread.run_sync(partial(_supersede, job.id, worker_id))
            return

    touched_paths: frozenset[str] = frozenset()
    path_aliases: dict[str, str] = {}
    if decision.eligible and review_run.needs_generation:
        if job.pull_request_id is None:
            raise NonRetryableError("Review job does not reference a pull request")
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
            touched_paths, path_aliases = _continuity_paths(update_diff)

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
                    path_aliases,
                    model_plan,
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
        if await anyio.to_thread.run_sync(
            partial(_mark_native_review_superseded, review_run.id, worker_id)
        ):
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
                else:
                    raise NonRetryableError(
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
        await _extend_publication_lease(job, worker_id)
        try:
            await _publish_native_thread_operations(
                event,
                review_run.id,
                job,
                worker_id,
            )
        except Exception:
            if check_run is not None:
                await anyio.to_thread.run_sync(
                    partial(_mark_native_check_failed, check_run.id)
                )
            raise
    if check_run is not None:
        await _extend_publication_lease(job, worker_id)
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
        raise NonRetryableError(f"Unsupported conversation workflow job: {job.job_type}")
    event = ReviewConversationEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise NonRetryableError("Conversation workflow identity does not match its payload")

    try:
        if not await anyio.to_thread.run_sync(
            partial(_heartbeat_lease, job.id, worker_id)
        ):
            raise RuntimeError("Workflow lease was lost before conversation processing")
        work = await anyio.to_thread.run_sync(partial(_begin_conversation, job.id))
        if work.root_comment_id != event.root_comment_id:
            raise NonRetryableError("Conversation thread does not match its workflow payload")
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
            else:
                raise NonRetryableError(
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
        raise NonRetryableError("Index workflow identity does not match its payload")

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
        raise NonRetryableError(f"Unsupported feedback workflow job: {job.job_type}")
    event = FeedbackSyncEvent.from_payload(job.payload)
    if (
        event.base_sha != job.base_revision
        or event.head_sha != job.revision
        or event.scope_key != job.scope_key
    ):
        raise NonRetryableError("Feedback workflow identity does not match its payload")

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
        else:
            raise NonRetryableError(
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
        raise NonRetryableError(f"Unsupported rule-learning workflow job: {job.job_type}")
    event = RuleLearningJobEvent.from_payload(job.payload)
    if (
        event.repository_id != job.repository_id
        or event.evidence_fingerprint != job.base_revision
        or event.evidence_fingerprint != job.revision
        or event.scope_key != job.scope_key
    ):
        raise NonRetryableError("Rule-learning workflow identity does not match its payload")

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
    raise NonRetryableError(f"Unsupported workflow job type: {job.job_type}")


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
            failure = terminal_review_failure(
                job.id,
                retries_exhausted=next_status == "dead",
            )
            try:
                await anyio.to_thread.run_sync(
                    partial(
                        _mark_native_review_terminal_failed,
                        job.id,
                        failure.error_code,
                    )
                )
            except Exception:
                LOGGER.exception(
                    "Failed to finalize terminal review lineage job=%s",
                    job.id,
                )
            try:
                event = PullRequestEvent.from_payload(job.payload)
            except Exception:
                event = None
                LOGGER.exception(
                    "Failed to decode the terminal review payload job=%s",
                    job.id,
                )
            if event is not None:
                LOGGER.error(
                    "Terminal review failure job=%s provider=%s repo=%s "
                    "number=%s head=%s error_code=%s",
                    job.id,
                    event.provider,
                    event.repo_full_name,
                    event.number,
                    event.head_sha,
                    failure.error_code,
                )
                try:
                    await _complete_existing_job_check(
                        job,
                        event,
                        conclusion="failure",
                        message=failure.summary,
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed to finalize terminal review check job=%s",
                        job.id,
                    )
                await _post_terminal_failure_notice(job, event, failure)
        LOGGER.exception(
            "Workflow job failed job=%s job_type=%s error_type=%s next_status=%s",
            job.id,
            job.job_type,
            type(error).__name__,
            next_status,
        )
    return True


def _heartbeat_seconds() -> float:
    """Interval between worker heartbeat lines. Zero disables them."""
    value = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "300"))
    if value < 0:
        raise ValueError("WORKER_HEARTBEAT_SECONDS must not be negative")
    return value


def _log_heartbeat(worker_id: str, processed: int) -> None:
    """Emit one liveness-and-progress line, never raising into the run loop.

    A heartbeat that can kill the worker it exists to observe would be worse
    than no heartbeat, so a database failure here is logged and swallowed --
    and the log line it produces is itself the signal that something is wrong.
    """
    try:
        with closing(get_conn()) as conn:
            depth = workflow_queue_depth(conn)
    except Exception:
        LOGGER.exception("Worker heartbeat could not read queue depth worker_id=%s", worker_id)
        return
    LOGGER.info(
        "Worker heartbeat worker_id=%s processed=%s queued=%s running=%s "
        "retrying=%s dead=%s failed=%s",
        worker_id,
        processed,
        depth.queued,
        depth.running,
        depth.retrying,
        depth.dead,
        depth.failed,
    )


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
    heartbeat_seconds = _heartbeat_seconds()
    next_feedback_schedule = 0.0
    next_learning_schedule = 0.0
    next_stranded_reconcile = 0.0
    # An idle worker used to emit nothing at all, so `docker compose logs worker`
    # was empty whether it was healthy, wedged, or had lost the database. The
    # worker has no container healthcheck on purpose -- process liveness says
    # nothing about progress -- which makes this the only signal that separates
    # the three, so it reports queue depth and work done rather than just "alive".
    next_heartbeat = 0.0
    processed_since_heartbeat = 0
    while True:
        now = time.monotonic()
        if heartbeat_seconds and now >= next_heartbeat:
            _log_heartbeat(worker_id, processed_since_heartbeat)
            processed_since_heartbeat = 0
            next_heartbeat = now + heartbeat_seconds
        if now >= next_stranded_reconcile:
            try:
                reconciled = await _finalize_stranded_reviews()
                if reconciled:
                    LOGGER.info(
                        "Reconciled stranded review jobs count=%s",
                        reconciled,
                    )
            except Exception:
                LOGGER.exception("Failed to reconcile stranded review jobs")
            next_stranded_reconcile = now + STRANDED_REVIEW_INTERVAL_SECONDS
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
        if claimed:
            processed_since_heartbeat += 1
        else:
            await anyio.sleep(poll_seconds)


def _probe_int(name: str) -> None:
    """Parse a plain integer variable the way the hot path parses it."""
    value = os.environ.get(name)
    if value is not None:
        int(value)


def _probe_positive_int(name: str) -> None:
    """Parse a variable the hot path requires to be a positive integer.

    The default handed to the shared parser is a placeholder, not the
    production default: an unset variable is valid by construction, so the
    probe only has to agree with production about what a *set* value may be.
    Repeating the real defaults here would create a second copy to drift.
    """
    _positive_int(name, 1)


def _probe_base_url(name: str, default: str) -> None:
    normalize_base_url(os.environ.get(name, default), field_name=name)


# Every configuration value the worker reads once a job has been claimed, in one
# place. `run_once` classifies ValueError as non-retryable -- deliberately, since
# NonRetryableError subclasses it -- so a bare env-parsing ValueError raised
# mid-job is indistinguishable from a deterministic fault: the job dead-letters
# on its first attempt and posts a terminal-failure comment, and it does that for
# every pull request in the fleet until the variable is fixed. Fixing it recovers
# nothing, because the jobs are already terminal. Resolving all of them here
# means one malformed value stops the worker at startup instead.
_CONFIGURATION_PROBES: tuple[tuple[str, object], ...] = (
    ("WORKFLOW_LEASE_SECONDS", _lease_seconds),
    # Read by the API process rather than the worker, but both run this
    # validator and a webhook is a bad place to discover a malformed integer.
    ("REVIEW_UPDATE_DEBOUNCE_SECONDS", review_update_debounce_seconds),
    ("MAX_CONTEXT_CHUNKS", max_context_chunks),
    ("MAX_CONTEXT_CHARS", max_context_chars),
    ("REVIEW_MODEL", review_model),
    ("REVIEW_VERIFIER_MODEL", review_verifier_model),
    ("REVIEW_EFFORT", review_effort),
    ("REVIEW_DEPTH", review_depth),
    ("REVIEW_PASSES", review_passes),
    ("MIN_REVIEW_CONFIDENCE", minimum_review_confidence),
    ("REVIEW_PROVENANCE_MIN_CONFIDENCE", review_provenance_minimum_confidence),
    ("REVIEW_STRUCTURED_OUTPUT_MODE", lambda: _supports_json_schema(review_model())),
    ("REVIEW_API_BASE", lambda: _model_api_base(review_model())),
    ("REVIEW_MAX_OUTPUT_TOKENS", partial(_probe_positive_int, "REVIEW_MAX_OUTPUT_TOKENS")),
    (
        "REVIEW_MODEL_TIMEOUT_SECONDS",
        partial(_probe_positive_int, "REVIEW_MODEL_TIMEOUT_SECONDS"),
    ),
    ("REVIEW_MODEL_RETRIES", model_retries),
    ("WORKER_HEARTBEAT_SECONDS", _heartbeat_seconds),
    (
        "REVIEW_DIFF_CHARS_PER_CALL",
        partial(_probe_positive_int, "REVIEW_DIFF_CHARS_PER_CALL"),
    ),
    ("REVIEW_MAX_DIFF_CHUNKS", partial(_probe_positive_int, "REVIEW_MAX_DIFF_CHUNKS")),
    ("REVIEW_DIAGRAM_DIFF_CHARS", partial(_probe_positive_int, "REVIEW_DIAGRAM_DIFF_CHARS")),
    (
        "REVIEW_DIAGRAM_CONTEXT_CHARS",
        partial(_probe_positive_int, "REVIEW_DIAGRAM_CONTEXT_CHARS"),
    ),
    (
        "REVIEW_DIAGRAM_MAX_OUTPUT_TOKENS",
        partial(_probe_positive_int, "REVIEW_DIAGRAM_MAX_OUTPUT_TOKENS"),
    ),
    ("SCM_API_TIMEOUT_SECONDS", scm_api_timeout_seconds),
    ("DIFFUSE_MAX_REPOSITORY_BYTES", max_repository_bytes),
    ("RULE_LEARNING_MODEL", rule_learning_model),
    (
        "RULE_LEARNING_MAX_OUTPUT_TOKENS",
        partial(_probe_positive_int, "RULE_LEARNING_MAX_OUTPUT_TOKENS"),
    ),
    (
        "RULE_LEARNING_MODEL_TIMEOUT_SECONDS",
        partial(_probe_positive_int, "RULE_LEARNING_MODEL_TIMEOUT_SECONDS"),
    ),
    ("SUGGESTED_RULE_MIN_SUPPORT", partial(_probe_int, "SUGGESTED_RULE_MIN_SUPPORT")),
    (
        "SUGGESTED_RULE_MIN_SUPPORT_PULL_REQUESTS",
        partial(_probe_int, "SUGGESTED_RULE_MIN_SUPPORT_PULL_REQUESTS"),
    ),
    # Scheduler settings. Not on a claimed job's path, but they are read by the
    # same process on a timer, and a startup failure beats a log line every pass.
    ("FEEDBACK_SYNC_INTERVAL_SECONDS", partial(_probe_int, "FEEDBACK_SYNC_INTERVAL_SECONDS")),
    ("FEEDBACK_SYNC_BATCH_SIZE", partial(_probe_int, "FEEDBACK_SYNC_BATCH_SIZE")),
    ("RULE_LEARNING_MIN_EVIDENCE", partial(_probe_int, "RULE_LEARNING_MIN_EVIDENCE")),
    (
        "RULE_LEARNING_MIN_PULL_REQUESTS",
        partial(_probe_int, "RULE_LEARNING_MIN_PULL_REQUESTS"),
    ),
    (
        "RULE_LEARNING_EVALUATION_INTERVAL_SECONDS",
        partial(_probe_int, "RULE_LEARNING_EVALUATION_INTERVAL_SECONDS"),
    ),
    ("RULE_LEARNING_BATCH_SIZE", partial(_probe_int, "RULE_LEARNING_BATCH_SIZE")),
    ("GITHUB_API_URL", partial(_probe_base_url, "GITHUB_API_URL", "https://api.github.com")),
    ("GITHUB_WEB_URL", partial(_probe_base_url, "GITHUB_WEB_URL", "https://github.com")),
    ("GitHub App authentication", validate_app_configuration),
)


def validate_worker_configuration() -> None:
    """Resolve every hot-path configuration value, naming the one that fails."""
    for name, resolve in _CONFIGURATION_PROBES:
        try:
            resolve()
        except ValueError as error:
            raise ValueError(f"{name} is invalid: {error}") from error


def validate_worker_model_controls() -> None:
    """Report what the configured models will be sent, and refuse the unhonorable.

    A third validator alongside the two below for the same reason they are
    separate from each other: this asks whether the *models* can express what
    the operator configured, which `validate_worker_configuration` -- a parse
    check -- cannot answer and should not grow to.

    Diffuse has no structured logging, no metrics, and no alerting, so a
    parameter dropped mid-review is indistinguishable from silence. Every
    resolution is reported here, before a single job is claimed, naming what was
    requested, what the model supports, and what will actually be sent. A
    candidate model LiteLLM *knows* cannot express the request does not start;
    see `ReviewDepthSupport.refusal` for why a model it knows nothing about is
    reported instead.

    This only ever sees the configured pair. `process_review_job` resolves the
    pair provenance routing actually chose, which startup cannot know.
    """

    support = resolve_review_depth_support()
    report_review_depth_support(support)
    refusal = support.refusal()
    if refusal is not None:
        raise ValueError(refusal)


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
    # The worker id is also the audit actor for anything the worker records, and
    # that column is bounded. Checking it here keeps an over-long WORKER_ID from
    # surfacing as a mid-review ValueError, which is the very hazard below.
    if not 1 <= len(args.worker_id.strip()) <= 255:
        parser.error("--worker-id must contain 1 to 255 characters")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        validate_worker_configuration()
        validate_worker_model_controls()
    except ValueError as error:
        parser.error(str(error))
    with closing(get_conn()) as conn:
        verify_database_current(conn)
    if args.once:
        anyio.run(run_once, args.worker_id)
    else:
        anyio.run(run_forever, args.worker_id, args.poll_seconds)


if __name__ == "__main__":
    main()
