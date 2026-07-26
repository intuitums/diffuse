from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from repository_policy.models import (
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
    TriggerSettingsPatch,
)
from repository_policy.resolve import resolve_review_policy
from retriever.context_models import CrossRepositoryContextPlan
from retriever.retrieve import RetrievedContextBundle
from service import worker
from service.approval_store import AutoApprovalHandle
from service.auto_approval import AutoApprovalDecision, AutoApprovalRisk
from service.check_store import CheckRunHandle
from service.conversation_store import (
    ConversationPublication,
    ConversationWork,
    PublishedConversationReply,
)
from service.feedback_models import ReactionSyncResult, ReviewReaction
from service.feedback_store import FeedbackSyncTarget
from service.finding_lineage import ReviewContinuity
from service.github_approval import PublishedApproval
from service.github_review import PublishedReview
from service.learning_models import (
    RuleLearningEvidence,
    RuleLearningJobEvent,
    RuleLearningResult,
    RuleLearningWork,
)
from service.review_engine import StructuredOutputValidationError
from service.review_models import (
    Category,
    ReviewFinding,
    ReviewReport,
    Severity,
)
from service.review_store import PublicationHandle, ReviewRunHandle
from service.scm import (
    FeedbackSyncEvent,
    ProviderPaginationLimitError,
    ProviderRateLimitError,
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
)
from service.workflow import NonRetryableError, WorkflowJob


def _event(**overrides) -> PullRequestEvent:
    payload = {
        "provider": "github",
        "scm_base_url": "https://github.com",
        "api_base_url": "https://api.github.com",
        "repo_full_name": "owner/repo",
        "number": 3,
        "web_url": "https://github.com/owner/repo/pull/3",
        "action": "opened",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "updated_at": "2026-07-23T15:30:00Z",
        "delivery_id": "delivery-3",
        "author": "octocat",
        "base_branch": "main",
        "head_branch": "feature/auth",
        "is_draft": False,
        "labels": ["needs-review"],
        "title": "Protect tenant boundaries",
        "description": "Adds authorization checks.",
        "trigger_kind": "automatic",
        "trigger_id": "",
        "metadata_complete": True,
        "changed_file_count": 1,
    }
    payload.update(overrides)
    return PullRequestEvent.from_payload(payload)


def _job(event: PullRequestEvent) -> WorkflowJob:
    return WorkflowJob(
        id=11,
        repository_id=2,
        pull_request_id=5,
        job_type="review_pull_request",
        scope_key=event.scope_key,
        base_revision=event.base_sha,
        revision=event.head_sha,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )


def _eligible_approval_decision() -> AutoApprovalDecision:
    return AutoApprovalDecision(
        eligible=True,
        reason_code="approved",
        message="All checks passed.",
        risk_level=AutoApprovalRisk.LOW,
        risk_ceiling=AutoApprovalRisk.LOW,
        changed_paths=("docs/guide.md",),
        changed_file_count=1,
        changed_line_count=2,
        diff_chars=120,
    )


@pytest.mark.anyio
async def test_native_auto_approval_dispatches_to_gitlab(monkeypatch):
    event = _event(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        web_url="https://gitlab.example.com/owner/repo/-/merge_requests/3",
    )
    decision = _eligible_approval_decision()
    published = PublishedApproval(
        external_id="gitlab:owner/repo:3:41:head",
        external_url=event.web_url,
    )
    github_publisher = AsyncMock()
    gitlab_publisher = AsyncMock(return_value=published)
    monkeypatch.setattr(worker, "publish_github_approval", github_publisher)
    monkeypatch.setattr(worker, "publish_gitlab_approval", gitlab_publisher)

    result = await worker._publish_native_auto_approval(
        event,
        review_run_id=42,
        decision=decision,
    )

    assert result == published
    github_publisher.assert_not_awaited()
    gitlab_publisher.assert_awaited_once_with(
        event,
        review_run_id=42,
        decision=decision,
    )


def _context_plan() -> CrossRepositoryContextPlan:
    return CrossRepositoryContextPlan(
        primary_repository_id=2,
        primary_repository_full_name="owner/repo",
        primary_snapshot_id=7,
        primary_commit_sha="a" * 40,
    )


def _status_check_policy():
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig(
                    version=1,
                    triggers=TriggerSettingsPatch(
                        status_check=True,
                        blocking_severities=("critical", "high"),
                    ),
                ),
            ),
        )
    )
    return resolve_review_policy(snapshot, ("app.py",))


def _conversation_event() -> ReviewConversationEvent:
    return ReviewConversationEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        number=3,
        delivery_id="conversation-1",
        external_comment_id="1201",
        root_comment_id="901",
        head_sha="a" * 40,
        base_sha="b" * 40,
        comment_commit_sha="a" * 40,
        author="reviewer",
        author_association="MEMBER",
        created_at="2026-07-23T17:00:00Z",
        question="Why can this bypass the tenant check?",
        file_path="app.py",
        line=1,
        side="RIGHT",
        diff_hunk="@@ -1 +1 @@\n+return account",
    )


def _conversation_job(event: ReviewConversationEvent) -> WorkflowJob:
    return WorkflowJob(
        id=13,
        repository_id=2,
        pull_request_id=5,
        job_type="answer_review_comment",
        scope_key=event.scope_key,
        base_revision=event.base_sha,
        revision=event.head_sha,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )


def _conversation_work() -> ConversationWork:
    return ConversationWork(
        id=81,
        status="generating",
        root_comment_id="901",
        finding=ReviewFinding(
            fingerprint="c" * 64,
            title="Missing tenant check",
            body="The lookup is not tenant scoped.",
            severity=Severity.HIGH,
            category=Category.SECURITY,
            confidence=0.95,
            file_path="app.py",
            line=1,
            side="RIGHT",
            evidence="The changed lookup accepts an arbitrary account ID.",
            suggested_fix="Add the authenticated tenant to the lookup.",
        ),
        previous_turns=(),
        answer=None,
        references=(),
        index_snapshot_id=None,
        external_reply_id=None,
        external_reply_url=None,
    )


def _feedback_event() -> FeedbackSyncEvent:
    return FeedbackSyncEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        number=3,
        root_comment_id="901",
        generation=2,
        base_sha="b" * 40,
        head_sha="a" * 40,
    )


def _rule_learning_event() -> RuleLearningJobEvent:
    return RuleLearningJobEvent(
        repository_id=2,
        repo_full_name="owner/repo",
        generation=1,
        evidence_fingerprint="d" * 64,
    )


def _rule_learning_job(event: RuleLearningJobEvent) -> WorkflowJob:
    return WorkflowJob(
        id=14,
        repository_id=event.repository_id,
        pull_request_id=None,
        job_type="generate_suggested_rules",
        scope_key=event.scope_key,
        base_revision=event.evidence_fingerprint,
        revision=event.evidence_fingerprint,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )


def _feedback_job(event: FeedbackSyncEvent) -> WorkflowJob:
    return WorkflowJob(
        id=14,
        repository_id=2,
        pull_request_id=5,
        job_type="sync_review_feedback",
        scope_key=event.scope_key,
        base_revision=event.base_sha,
        revision=event.head_sha,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )


@pytest.mark.anyio
async def test_worker_checks_revision_before_review_and_completes(monkeypatch):
    event = _event(action="synchronize")
    job = _job(event)
    current_checks = iter([True, True, True, True, True])
    generated: list[tuple] = []
    publication_results: list[tuple] = []
    report = ReviewReport(
        summary="No issues.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig(
                        version=1,
                        triggers=TriggerSettingsPatch(review_updates=True),
                    ),
                ),
            )
        ),
        ("app.py",),
    )
    continuity = ReviewContinuity()
    published_review = PublishedReview(
        external_id="61",
        external_url="https://example/review/61",
    )

    monkeypatch.setattr(
        worker,
        "fetch_pull_request_diff",
        AsyncMock(return_value="diff --git a/app.py b/app.py"),
    )
    monkeypatch.setattr(
        worker,
        "compatible_snapshot_id",
        lambda *_args: 7,
    )
    monkeypatch.setattr(
        worker,
        "_load_review_policy",
        lambda *_args: policy,
    )
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_args: _context_plan(),
    )
    monkeypatch.setattr(
        worker,
        "retrieve_context_from_plan",
        lambda *_args: RetrievedContextBundle(
            snapshot_id=7,
            contexts=(),
            context_plan=_context_plan(),
        ),
    )
    monkeypatch.setattr(
        worker,
        "_heartbeat_and_check_current",
        lambda *_args: next(current_checks),
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_args: ReviewRunHandle(id=41, status="generating", index_snapshot_id=7),
    )
    monkeypatch.setattr(
        worker,
        "_latest_native_review_head",
        lambda *_args: "c" * 40,
    )
    update_diff = AsyncMock(
        return_value=(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
    )
    monkeypatch.setattr(worker, "fetch_pull_request_update_diff", update_diff)
    monkeypatch.setattr(
        worker,
        "_generate_and_persist_review",
        lambda *args: generated.append(args),
    )
    monkeypatch.setattr(worker, "_load_native_report", lambda *_args: report)
    monkeypatch.setattr(
        worker,
        "_load_native_continuity",
        lambda *_args: continuity,
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_publication",
        lambda *_args: PublicationHandle(
            id=51,
            status="publishing",
            idempotency_key="review-run:41:pull-request-review",
            external_id=None,
            external_url=None,
        ),
    )
    monkeypatch.setattr(
        worker,
        "publish_github_review",
        AsyncMock(return_value=published_review),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_publication_published",
        lambda *args: publication_results.append(args),
    )
    monkeypatch.setattr(
        worker,
        "_publish_native_thread_operations",
        AsyncMock(),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_review_job(job, "worker-1")

    assert generated == [
        (
            job,
            41,
            "diff --git a/app.py b/app.py",
            [],
            "worker-1",
            policy,
            frozenset({"app.py"}),
            {},
        )
    ]
    worker.publish_github_review.assert_awaited_once_with(
        event,
        review_run_id=41,
        report=report,
        review_number=1,
        continuity=continuity,
    )
    update_diff.assert_awaited_once_with(event, "c" * 40)
    assert publication_results == [
        (51, 41, "github", published_review),
    ]


@pytest.mark.anyio
async def test_worker_auto_approves_only_after_clean_review_publication(
    monkeypatch,
):
    event = _event(
        title="Clarify the guide",
        description="Documentation only.",
        head_branch="docs",
    )
    job = _job(event)
    diff_text = (
        "diff --git a/docs/guide.md b/docs/guide.md\n"
        "--- a/docs/guide.md\n"
        "+++ b/docs/guide.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "auto_approval": {"enabled": True},
                        }
                    ),
                ),
            )
        ),
        ("docs/guide.md",),
    )
    report = ReviewReport(
        summary="No issues.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )
    continuity = ReviewContinuity()
    approval_handle = AutoApprovalHandle(
        id=91,
        status="publishing",
        eligible=True,
        idempotency_key=(
            f"review-run:41:auto-approval:{event.head_sha}"
        ),
        external_id=None,
        external_url=None,
    )
    published_approval = PublishedApproval(
        external_id="101",
        external_url="https://example/review/101",
    )
    decisions: list[tuple] = []
    published: list[tuple] = []

    monkeypatch.setattr(
        worker,
        "fetch_pull_request_diff",
        AsyncMock(return_value=diff_text),
    )
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_args: 7)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_args: policy)
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_args: _context_plan(),
    )
    monkeypatch.setattr(
        worker,
        "_heartbeat_and_check_current",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_args: ReviewRunHandle(
            id=41,
            status="ready",
            index_snapshot_id=7,
        ),
    )
    monkeypatch.setattr(worker, "_load_native_report", lambda *_args: report)
    monkeypatch.setattr(
        worker,
        "_load_native_continuity",
        lambda *_args: continuity,
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_publication",
        lambda *_args: PublicationHandle(
            id=51,
            status="published",
            idempotency_key="review-run:41:pull-request-review",
            external_id="61",
            external_url="https://example/review/61",
        ),
    )
    monkeypatch.setattr(
        worker,
        "_publish_native_thread_operations",
        AsyncMock(),
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_auto_approval",
        lambda *args: decisions.append(args) or approval_handle,
    )
    monkeypatch.setattr(
        worker,
        "publish_github_approval",
        AsyncMock(return_value=published_approval),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_auto_approval_published",
        lambda *args: published.append(args),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_review_job(job, "worker-1")

    assert len(decisions) == 1
    review_run_id, approval_event, policy_fingerprint, decision = decisions[0]
    assert review_run_id == 41
    assert approval_event == event
    assert policy_fingerprint == policy.fingerprint
    assert decision.eligible
    worker.publish_github_approval.assert_awaited_once_with(
        event,
        review_run_id=41,
        decision=decision,
    )
    assert published == [(91, published_approval)]


@pytest.mark.anyio
async def test_worker_supersedes_before_review_when_head_changed(monkeypatch):
    event = _event()
    superseded: list[tuple] = []
    fetch = AsyncMock(return_value="diff --git a/app.py b/app.py")

    monkeypatch.setattr(worker, "fetch_pull_request_diff", fetch)
    monkeypatch.setattr(worker, "_heartbeat_and_check_current", lambda *_args: False)
    monkeypatch.setattr(
        worker,
        "_complete_existing_job_check",
        AsyncMock(),
    )
    monkeypatch.setattr(
        worker,
        "_supersede",
        lambda *args: superseded.append(args) or True,
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_args: pytest.fail("stale review must not run"),
    )

    await worker.process_review_job(_job(event), "worker-1")

    assert superseded == [(11, "worker-1")]
    fetch.assert_not_awaited()


@pytest.mark.anyio
async def test_worker_publishes_exact_review_status_check(monkeypatch):
    event = _event()
    job = _job(event)
    current_checks = iter([True, True, True, True, True, True])
    policy = _status_check_policy()
    report = ReviewReport(
        summary="No blocking issues.",
        risk_score=1,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )
    continuity = ReviewContinuity()
    check_run = CheckRunHandle(
        id=71,
        status="in_progress",
        external_key="diffuse-review-run:41",
        external_id="81",
        external_url="https://example/check/81",
        conclusion=None,
    )

    monkeypatch.setattr(
        worker,
        "fetch_pull_request_diff",
        AsyncMock(return_value="diff --git a/app.py b/app.py"),
    )
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_args: 7)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_args: policy)
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_args: _context_plan(),
    )
    monkeypatch.setattr(
        worker,
        "_heartbeat_and_check_current",
        lambda *_args: next(current_checks),
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_args: ReviewRunHandle(
            id=41,
            status="ready",
            index_snapshot_id=7,
        ),
    )
    monkeypatch.setattr(worker, "_load_native_report", lambda *_args: report)
    monkeypatch.setattr(
        worker,
        "_load_native_continuity",
        lambda *_args: continuity,
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_publication",
        lambda *_args: PublicationHandle(
            id=51,
            status="published",
            idempotency_key="review-run:41:pull-request-review",
            external_id="61",
            external_url="https://example/review/61",
        ),
    )
    ensure_check = AsyncMock(return_value=check_run)
    complete_check = AsyncMock()
    monkeypatch.setattr(worker, "_ensure_native_check", ensure_check)
    monkeypatch.setattr(worker, "_complete_native_check", complete_check)
    monkeypatch.setattr(
        worker,
        "_publish_native_thread_operations",
        AsyncMock(),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_review_job(job, "worker-1")

    ensure_check.assert_awaited_once_with(event, 41)
    complete_check.assert_awaited_once_with(
        event,
        check_run,
        conclusion="success",
        blocking_severities=("critical", "high"),
        report=report,
        unresolved_findings=(),
    )


@pytest.mark.anyio
async def test_worker_persists_trigger_skip_without_retrieval_or_publication(monkeypatch):
    event = _event(is_draft=True)
    job = _job(event)
    current_checks = iter([True, True, True, True, True])
    policy = resolve_review_policy(RepositoryPolicySnapshot(), ("app.py",))
    skipped: list[tuple] = []
    report = ReviewReport(
        summary="Automatic review skipped for a draft pull request.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=0,
        ignored_file_count=0,
        inline_comments_enabled=False,
        publication_enabled=False,
        skip_reason="draft_pull_request",
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )

    monkeypatch.setattr(
        worker,
        "fetch_pull_request_diff",
        AsyncMock(return_value="diff --git a/app.py b/app.py"),
    )
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_args: 7)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_args: policy)
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_args: _context_plan(),
    )
    monkeypatch.setattr(
        worker,
        "_heartbeat_and_check_current",
        lambda *_args: next(current_checks),
    )
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_args: ReviewRunHandle(id=42, status="generating", index_snapshot_id=7),
    )
    monkeypatch.setattr(
        worker,
        "_persist_trigger_skip",
        lambda *args: skipped.append(args),
    )
    monkeypatch.setattr(
        worker,
        "retrieve_context_from_plan",
        lambda *_args: pytest.fail("skipped triggers must not retrieve context"),
    )
    monkeypatch.setattr(
        worker,
        "_generate_and_persist_review",
        lambda *_args: pytest.fail("skipped triggers must not call the review model"),
    )
    monkeypatch.setattr(worker, "_load_native_report", lambda *_args: report)
    monkeypatch.setattr(
        worker,
        "_begin_native_publication",
        lambda *_args: pytest.fail("skipped triggers must not publish"),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_review_job(job, "worker-1")

    assert len(skipped) == 1
    assert skipped[0][0] == 42
    assert skipped[0][3].reason_code == "draft_pull_request"


@pytest.mark.anyio
async def test_conversation_worker_retrieves_generates_and_publishes_once(monkeypatch):
    event = _conversation_event()
    job = _conversation_job(event)
    work = _conversation_work()
    policy = resolve_review_policy(RepositoryPolicySnapshot(), ("app.py",))
    generated = []
    published = []
    result = PublishedConversationReply(
        external_id="1301",
        external_url="https://example/comment/1301",
    )
    heartbeat = iter([True, True])

    monkeypatch.setattr(worker, "_heartbeat_lease", lambda *_args: next(heartbeat))
    monkeypatch.setattr(worker, "_begin_conversation", lambda *_args: work)
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_args: 7)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_args: policy)
    monkeypatch.setattr(
        worker,
        "retrieve_context_from_snapshot",
        lambda *_args: RetrievedContextBundle(snapshot_id=7, contexts=()),
    )
    monkeypatch.setattr(
        worker,
        "_generate_and_persist_conversation",
        lambda *args: generated.append(args),
    )
    monkeypatch.setattr(
        worker,
        "_begin_conversation_publication",
        lambda *_args: ConversationPublication(
            id=81,
            status="publishing",
            answer="The lookup lacks a tenant predicate.",
            references=(),
            external_reply_id=None,
            external_reply_url=None,
        ),
    )
    monkeypatch.setattr(
        worker,
        "publish_github_conversation_reply",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        worker,
        "_mark_conversation_published",
        lambda *args: published.append(args),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_conversation_job(job, "worker-1")

    assert generated == [
        (job, event, work, [], 7, "worker-1"),
    ]
    worker.publish_github_conversation_reply.assert_awaited_once_with(
        event,
        answer="The lookup lacks a tenant predicate.",
        references=(),
    )
    assert published == [(81, result)]


@pytest.mark.anyio
async def test_conversation_worker_honors_path_scoped_disable(monkeypatch):
    event = _conversation_event()
    job = _conversation_job(event)
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "review": {"respond_to_comments": False},
                        }
                    ),
                ),
            )
        ),
        ("app.py",),
    )
    ignored = []

    monkeypatch.setattr(worker, "_heartbeat_lease", lambda *_args: True)
    monkeypatch.setattr(worker, "_begin_conversation", lambda *_args: _conversation_work())
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_args: 7)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_args: policy)
    monkeypatch.setattr(
        worker,
        "_ignore_conversation",
        lambda *args: ignored.append(args),
    )
    monkeypatch.setattr(
        worker,
        "retrieve_context_from_snapshot",
        lambda *_args: pytest.fail("disabled conversation must not retrieve"),
    )
    monkeypatch.setattr(
        worker,
        "publish_github_conversation_reply",
        AsyncMock(),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_conversation_job(job, "worker-1")

    assert ignored == [(81, "conversation_disabled")]
    worker.publish_github_conversation_reply.assert_not_awaited()


@pytest.mark.anyio
async def test_feedback_worker_reconciles_reactions_and_completes(monkeypatch):
    event = _feedback_event()
    job = _feedback_job(event)
    target = FeedbackSyncTarget(
        finding_thread_id=81,
        repository_id=2,
        pull_request_id=5,
        finding_id=61,
        root_comment_id="901",
        generation=2,
        finding_category="maintainability",
        finding_severity="medium",
    )
    reactions = (
        ReviewReaction(
            external_id="1001",
            actor_login="reviewer",
            content="-1",
            created_at="2026-07-23T18:00:00Z",
        ),
    )
    sync_result = ReactionSyncResult(
        observed=1,
        withdrawn=0,
        active_positive=0,
        active_negative=1,
    )
    reconciled = []

    monkeypatch.setattr(worker, "_heartbeat_lease", lambda *_args: True)
    monkeypatch.setattr(worker, "_begin_feedback_sync", lambda *_args: target)
    monkeypatch.setattr(
        worker,
        "fetch_github_review_reactions",
        AsyncMock(return_value=reactions),
    )
    monkeypatch.setattr(
        worker,
        "_reconcile_feedback_reactions",
        lambda *args: reconciled.append(args) or sync_result,
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)
    failed = []
    monkeypatch.setattr(
        worker,
        "_mark_feedback_sync_failed",
        lambda *args: failed.append(args),
    )

    await worker.process_feedback_sync_job(job, "worker-1")

    worker.fetch_github_review_reactions.assert_awaited_once_with(event)
    assert reconciled == [(target, reactions)]
    assert failed == []


@pytest.mark.anyio
async def test_rule_learning_worker_generates_persists_and_completes(monkeypatch):
    event = _rule_learning_event()
    job = _rule_learning_job(event)
    work = RuleLearningWork(
        run_id=91,
        status="generating",
        evidence=(
            RuleLearningEvidence(
                event_id=7,
                pull_request_id=5,
                pull_request_number=3,
                source_kind="reply",
                signal_kind="context",
                content="Use the shared validator.",
                finding_title="Validate the request",
                finding_body="The request is not validated.",
                file_path="app.py",
                category="api",
                severity="high",
                suppression_protected=False,
            ),
        ),
        evidence_fingerprint=event.evidence_fingerprint,
    )
    result = RuleLearningResult(proposed=1, consolidated=0, rejected_candidates=0)
    generated = []
    failed = []

    monkeypatch.setattr(worker, "_heartbeat_lease", lambda *_args: True)
    monkeypatch.setattr(worker, "_begin_rule_learning_job", lambda *_args: work)
    monkeypatch.setattr(
        worker,
        "_generate_and_persist_rule_learning",
        lambda *args: generated.append(args) or result,
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)
    monkeypatch.setattr(
        worker,
        "_mark_rule_learning_job_failed",
        lambda *args: failed.append(args),
    )

    await worker.process_rule_learning_job(job, "worker-1")

    assert generated == [(job, work, "worker-1")]
    assert failed == []


@pytest.mark.anyio
async def test_index_worker_dispatches_exact_push_revision(monkeypatch):
    event = PushEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="owner/repo",
        ref_name="refs/heads/main",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        pushed_at="2026-07-23T15:30:00Z",
        delivery_id="push-1",
    )
    job = WorkflowJob(
        id=12,
        repository_id=2,
        pull_request_id=None,
        job_type="index_repository",
        scope_key=event.scope_key,
        base_revision=event.after_sha,
        revision=event.after_sha,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )
    indexed = []
    latest_checks = iter([True, True])

    monkeypatch.setattr(
        worker,
        "_heartbeat_and_check_latest",
        lambda *_args: next(latest_checks),
    )
    monkeypatch.setattr(
        worker,
        "_index_repository_job",
        lambda *args: indexed.append(args),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_args: True)

    await worker.process_index_job(job, "worker-1")

    assert indexed == [(job, event, "worker-1")]


@pytest.mark.anyio
async def test_terminal_review_failure_completes_existing_status_check(monkeypatch):
    event = _event()
    job = _job(event)
    finalize = AsyncMock()

    monkeypatch.setattr(worker, "_claim", lambda *_args: job)
    monkeypatch.setattr(
        worker,
        "process_job",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    )
    monkeypatch.setattr(worker, "_fail", lambda *_args, **_kwargs: "dead")
    terminal_failure = []
    monkeypatch.setattr(
        worker,
        "_mark_native_review_terminal_failed",
        lambda *args: terminal_failure.append(args),
    )
    monkeypatch.setattr(worker, "_complete_existing_job_check", finalize)

    assert await worker.run_once("worker-1")

    assert terminal_failure == [(job.id,)]
    finalize.assert_awaited_once_with(
        job,
        event,
        conclusion="failure",
        message=(
            "Diffuse could not complete this review after exhausting "
            "its retry policy."
        ),
    )


def _capture_failure_classification(monkeypatch, job, error):
    """Drive run_once through a failing job and capture how it was classified."""
    recorded: dict = {}

    def fail(
        _job_id,
        _worker_id,
        *,
        retryable,
        retry_at=None,
        error_code="workflow_processing_failed",
    ):
        recorded["retryable"] = retryable
        recorded["retry_at"] = retry_at
        recorded["error_code"] = error_code
        return "queued" if retryable else "failed"

    monkeypatch.setattr(worker, "_claim", lambda *_args: job)
    monkeypatch.setattr(worker, "process_job", AsyncMock(side_effect=error))
    monkeypatch.setattr(worker, "_fail", fail)
    monkeypatch.setattr(
        worker,
        "_mark_native_review_terminal_failed",
        lambda *_args: None,
    )
    monkeypatch.setattr(worker, "_complete_existing_job_check", AsyncMock())
    return recorded


@pytest.mark.anyio
async def test_malformed_model_output_is_retried(monkeypatch):
    """A truncated or drifting model response is transient, not terminal."""
    job = _job(_event())
    error = StructuredOutputValidationError(
        prompt_tokens=10,
        completion_tokens=2,
    )
    recorded = _capture_failure_classification(monkeypatch, job, error)

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": True,
        "retry_at": None,
        "error_code": "workflow_processing_failed",
    }


@pytest.mark.anyio
async def test_embedding_dimension_mismatch_is_retried(monkeypatch):
    """Provider vector-width drift must retry instead of permanently failing."""
    job = _job(_event())
    recorded = _capture_failure_classification(
        monkeypatch,
        job,
        RuntimeError("Embedding model returned 1024 dimensions; expected 1536"),
    )

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": True,
        "retry_at": None,
        "error_code": "workflow_processing_failed",
    }


@pytest.mark.anyio
async def test_deterministic_faults_are_not_retried(monkeypatch):
    job = _job(_event())
    recorded = _capture_failure_classification(
        monkeypatch,
        job,
        ValueError("REVIEW_STRUCTURED_OUTPUT_MODE is invalid"),
    )

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": False,
        "retry_at": None,
        "error_code": "workflow_processing_failed",
    }


@pytest.mark.anyio
async def test_pagination_exhaustion_is_not_retried(monkeypatch):
    """Re-reading an oversized listing four more times cannot change the answer.

    Classified retryable, an idempotency scan that outgrew its page budget
    fails identically on every attempt, exhausts the retry policy and lands on
    the pull request as a terminal red X — permanently, because the listing
    only ever gets longer. It has to fail once, non-retryably, with a message
    an operator can act on.
    """
    job = _job(_event())
    recorded = _capture_failure_classification(
        monkeypatch,
        job,
        ProviderPaginationLimitError("github", "pull-request reviews", pages=20),
    )

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": False,
        "retry_at": None,
        "error_code": "provider_pagination_exhausted",
    }


@pytest.mark.anyio
async def test_rate_limited_review_defers_instead_of_posting_a_red_x(monkeypatch):
    """A quota window must park the job, not spend attempts and fail the check."""
    job = _job(_event())
    retry_at = datetime.now(tz=UTC) + timedelta(minutes=45)
    finalize = AsyncMock()
    error = ProviderRateLimitError(
        "github rate limited Diffuse",
        provider="github",
        retry_at=retry_at,
    )
    recorded = _capture_failure_classification(monkeypatch, job, error)
    monkeypatch.setattr(worker, "_complete_existing_job_check", finalize)

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": True,
        "retry_at": retry_at,
        "error_code": "workflow_processing_failed",
    }
    finalize.assert_not_awaited()


@pytest.mark.anyio
async def test_wrapped_rate_limit_still_carries_the_reset_instant(monkeypatch):
    """Provider helpers re-raise as RuntimeError; the reset must survive that."""
    job = _job(_event())
    retry_at = datetime.now(tz=UTC) + timedelta(minutes=20)
    try:
        raise ProviderRateLimitError(
            "gitlab rate limited Diffuse",
            provider="gitlab",
            retry_at=retry_at,
        )
    except ProviderRateLimitError as cause:
        error = RuntimeError("GitLab review publication failed")
        error.__cause__ = cause
    recorded = _capture_failure_classification(monkeypatch, job, error)

    assert await worker.run_once("worker-1")

    assert recorded == {
        "retryable": True,
        "retry_at": retry_at,
        "error_code": "workflow_processing_failed",
    }


@pytest.mark.anyio
async def test_non_retryable_failure_check_does_not_claim_exhausted_retries(
    monkeypatch,
):
    event = _event()
    job = _job(event)
    finalize = AsyncMock()

    monkeypatch.setattr(worker, "_claim", lambda *_args: job)
    monkeypatch.setattr(
        worker,
        "process_job",
        AsyncMock(side_effect=NonRetryableError("Unsupported SCM provider: svn")),
    )
    monkeypatch.setattr(worker, "_fail", lambda *_args, **_kwargs: "failed")
    monkeypatch.setattr(
        worker,
        "_mark_native_review_terminal_failed",
        lambda *_args: None,
    )
    monkeypatch.setattr(worker, "_complete_existing_job_check", finalize)

    assert await worker.run_once("worker-1")

    message = finalize.await_args.kwargs["message"]
    assert "exhausting" not in message
    assert "retrying cannot resolve" in message


class _LivenessConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None


def test_claiming_and_heartbeating_record_worker_progress(monkeypatch, tmp_path):
    # `restart: unless-stopped` only catches a worker that exits, so the probe
    # needs a signal that a wedged-but-running worker stops refreshing.
    liveness = tmp_path / "diffuse-worker-alive"
    monkeypatch.setenv("DIFFUSE_WORKER_LIVENESS_FILE", str(liveness))
    monkeypatch.setattr(worker, "get_conn", _LivenessConnection)
    monkeypatch.setattr(worker, "claim_workflow_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "heartbeat_workflow_job",
        lambda *_args, **_kwargs: True,
    )

    assert worker._claim("worker-1") is None
    assert liveness.exists()

    liveness.unlink()
    assert worker._heartbeat_lease(1, "worker-1")
    assert liveness.exists()
