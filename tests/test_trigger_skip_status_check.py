"""DEV-306: trigger-skipped reviews must still publish a terminal status check."""

from __future__ import annotations

from dataclasses import replace

import pytest

from repository_policy.models import (
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
    TriggerSettingsPatch,
)
from repository_policy.resolve import resolve_review_policy
from retriever.context_models import CrossRepositoryContextPlan
from service.finding_lineage import ReviewContinuity
from service.hosted.workflow import WorkflowJob
from service.models.review import ReviewReport
from service.review.engine import ReviewDepthSupport
from service.scm import PullRequestEvent
from service.storage.check import CheckRunHandle
from service.storage.review import ReviewRunHandle


def _bare_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 7,
            "web_url": "https://github.com/owner/repo/pull/7",
            "action": "synchronize",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
        }
    )


def _event(**overrides) -> PullRequestEvent:
    defaults = {
        "is_draft": False,
        "action": "synchronize",
        "trigger_kind": "automatic",
        "metadata_complete": True,
        "author": "octocat",
        "base_branch": "main",
        "head_branch": "feature/next",
        "title": "Follow-up commit",
        "description": "",
        "labels": (),
        "changed_file_count": 1,
    }
    return replace(_bare_event(), **{**defaults, **overrides})


def _job(event: PullRequestEvent) -> WorkflowJob:
    return WorkflowJob(
        id=99,
        repository_id=3,
        pull_request_id=11,
        job_type="review_pull_request",
        scope_key=event.scope_key,
        base_revision=event.base_sha,
        revision=event.head_sha,
        payload=event.to_payload(),
        attempt_count=1,
        max_attempts=5,
    )


def _policy(*, status_check: bool, review_updates: bool = False):
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig(
                    version=1,
                    triggers=TriggerSettingsPatch(
                        status_check=status_check,
                        review_updates=review_updates,
                    ),
                ),
            ),
        )
    )
    return resolve_review_policy(snapshot, {"app.py"})


DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


@pytest.mark.anyio
async def test_ineligible_synchronize_still_creates_and_completes_skipped_check(
    monkeypatch,
):
    """status_check on + review_updates off must not leave head B checkless."""
    from service.hosted import worker

    event = _event()
    job = _job(event)
    policy = _policy(status_check=True, review_updates=False)
    ensure_calls: list[tuple[str, int]] = []
    complete_calls: list[dict] = []
    persisted: list[ReviewReport] = []

    async def fake_ensure(evt, review_run_id):
        ensure_calls.append((evt.head_sha, review_run_id))
        return CheckRunHandle(
            id=501,
            status="in_progress",
            external_key=f"diffuse-review-run:{review_run_id}",
            external_id="777",
            external_url="https://example/check/777",
            conclusion=None,
        )

    async def fake_complete(evt, handle, **kwargs):
        complete_calls.append(
            {
                "head_sha": evt.head_sha,
                "handle_id": None if handle is None else handle.id,
                "conclusion": kwargs.get("conclusion"),
                "report": kwargs.get("report"),
            }
        )

    def fake_persist(review_run_id, diff_text, resolved_policy, decision):
        report = ReviewReport(
            summary=decision.message,
            risk_score=0,
            findings=[],
            diff_file_count=1,
            reviewed_file_count=0,
            ignored_file_count=0,
            inline_comments_enabled=False,
            publication_enabled=False,
            skip_reason=decision.reason_code,
            context_chunk_count=0,
            prompt_tokens=0,
            completion_tokens=0,
        )
        persisted.append(report)

    async def fake_fetch(_evt):
        return DIFF

    monkeypatch.setenv("REVIEW_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setattr(worker, "_heartbeat_and_check_current", lambda *_a: True)
    monkeypatch.setattr(worker, "_fetch_scm_pull_request_diff", fake_fetch)
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 41)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_a: policy)
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_a: CrossRepositoryContextPlan(
            primary_repository_id=3,
            primary_repository_full_name="owner/repo",
            primary_snapshot_id=41,
            primary_commit_sha="c" * 40,
        ),
    )
    monkeypatch.setattr(
        worker,
        "resolve_review_depth_support",
        lambda **_k: ReviewDepthSupport(depth=None, variable="REVIEW_DEPTH", plans=()),
    )
    monkeypatch.setattr(worker, "report_review_depth_support", lambda *_a: None)
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_a: ReviewRunHandle(id=42, status="generating", index_snapshot_id=41),
    )
    monkeypatch.setattr(worker, "_persist_trigger_skip", fake_persist)
    monkeypatch.setattr(worker, "_ensure_native_check", fake_ensure)
    monkeypatch.setattr(worker, "_complete_native_check", fake_complete)
    monkeypatch.setattr(worker, "_load_native_report", lambda *_a: persisted[0])
    monkeypatch.setattr(worker, "_load_native_continuity", lambda *_a: ReviewContinuity())
    monkeypatch.setattr(worker, "_complete", lambda *_a: True)
    monkeypatch.setattr(worker, "_supersede", lambda *_a: None)

    await worker.process_review_job(job, "worker-1")

    assert ensure_calls == [("a" * 40, 42)]
    assert len(persisted) == 1
    assert persisted[0].publication_enabled is False
    assert persisted[0].skip_reason == "updates_disabled"
    assert len(complete_calls) == 1
    assert complete_calls[0]["conclusion"] == "skipped"
    assert complete_calls[0]["handle_id"] == 501
    assert complete_calls[0]["head_sha"] == "a" * 40
    assert "disabled" in complete_calls[0]["report"].summary.lower()


@pytest.mark.anyio
async def test_status_check_off_still_skips_check_creation_on_ineligible(monkeypatch):
    from service.hosted import worker

    event = _event()
    job = _job(event)
    policy = _policy(status_check=False, review_updates=False)
    ensure_calls: list = []

    async def fake_ensure(*_a, **_k):
        ensure_calls.append(True)
        raise AssertionError("check must not be created when status_check is off")

    async def fake_fetch(_evt):
        return DIFF

    monkeypatch.setenv("REVIEW_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setattr(worker, "_heartbeat_and_check_current", lambda *_a: True)
    monkeypatch.setattr(worker, "_fetch_scm_pull_request_diff", fake_fetch)
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 41)
    monkeypatch.setattr(worker, "_load_review_policy", lambda *_a: policy)
    monkeypatch.setattr(
        worker,
        "_load_cross_repository_context_plan",
        lambda *_a: CrossRepositoryContextPlan(
            primary_repository_id=3,
            primary_repository_full_name="owner/repo",
            primary_snapshot_id=41,
            primary_commit_sha="c" * 40,
        ),
    )
    monkeypatch.setattr(
        worker,
        "resolve_review_depth_support",
        lambda **_k: ReviewDepthSupport(depth=None, variable="REVIEW_DEPTH", plans=()),
    )
    monkeypatch.setattr(worker, "report_review_depth_support", lambda *_a: None)
    monkeypatch.setattr(
        worker,
        "_begin_native_review",
        lambda *_a: ReviewRunHandle(id=42, status="generating", index_snapshot_id=41),
    )
    monkeypatch.setattr(
        worker,
        "_persist_trigger_skip",
        lambda *_a: None,
    )
    monkeypatch.setattr(worker, "_ensure_native_check", fake_ensure)
    monkeypatch.setattr(
        worker,
        "_load_native_report",
        lambda *_a: ReviewReport(
            summary="skipped",
            risk_score=0,
            findings=[],
            diff_file_count=1,
            reviewed_file_count=0,
            publication_enabled=False,
            skip_reason="updates_disabled",
            context_chunk_count=0,
            prompt_tokens=0,
            completion_tokens=0,
        ),
    )
    monkeypatch.setattr(worker, "_complete", lambda *_a: True)

    await worker.process_review_job(job, "worker-1")

    assert ensure_calls == []


def test_skipped_check_output_title_is_not_passed():
    from service.github.check import _completion_output

    report = ReviewReport(
        summary="Automatic review on new commits is disabled by repository policy.",
        risk_score=0,
        findings=[],
        diff_file_count=1,
        reviewed_file_count=0,
        publication_enabled=False,
        skip_reason="updates_disabled",
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )
    output = _completion_output(report, "skipped", ("critical", "high"), None, ())
    assert output["title"] == "Diffuse review skipped"
    assert "disabled" in output["summary"].lower()
