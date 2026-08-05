"""DEV-313: transient check completion failure must stay reclaimable."""

from __future__ import annotations

import pytest

from service.hosted import worker
from service.hosted.workflow import StrandedReviewJob
from service.scm import PullRequestEvent
from service.storage.check import CheckRunHandle


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 13,
            "web_url": "https://github.com/owner/repo/pull/13",
            "action": "synchronize",
            "head_sha": "c" * 40,
            "base_sha": "d" * 40,
            "updated_at": "2026-08-03T11:00:00Z",
            "delivery_id": "delivery-313",
        }
    )


def _handle(*, status: str = "in_progress") -> CheckRunHandle:
    return CheckRunHandle(
        id=31,
        status=status,
        external_key="diffuse-review-run:77",
        external_id="911",
        external_url="https://example/check/911",
        conclusion=None,
    )


@pytest.mark.anyio
async def test_complete_native_check_patch_timeout_leaves_row_reclaimable(
    monkeypatch,
):
    """A PATCH timeout must not mark durable failed when external_id is known."""
    event = _event()
    handle = _handle()
    completing: list[int] = []
    failed: list[int] = []
    completed: list[tuple[int, str]] = []

    async def fake_complete(*_a, **_k):
        raise TimeoutError("GitHub check PATCH timed out")

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    monkeypatch.setattr(
        worker,
        "_mark_native_check_completing",
        lambda check_run_id: completing.append(check_run_id),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_check_failed",
        lambda check_run_id: failed.append(check_run_id),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_check_completed",
        lambda check_run_id, conclusion: completed.append(
            (check_run_id, conclusion)
        ),
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await worker._complete_native_check(
            event,
            handle,
            conclusion="failure",
            message="Diffuse could not finish this review.",
        )

    assert completing == [31]
    assert failed == []
    assert completed == []


@pytest.mark.anyio
async def test_complete_native_check_retries_after_prior_failed_status(
    monkeypatch,
):
    """Store already allows failed→completing; completer must use that path."""
    event = _event()
    handle = _handle(status="failed")
    completing: list[int] = []
    completed: list[tuple[int, str]] = []
    failed: list[int] = []
    patched: list[str] = []

    async def fake_complete(_event, **kwargs):
        patched.append(kwargs["external_id"])
        assert kwargs["conclusion"] == "failure"

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    monkeypatch.setattr(
        worker,
        "_mark_native_check_completing",
        lambda check_run_id: completing.append(check_run_id),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_check_completed",
        lambda check_run_id, conclusion: completed.append(
            (check_run_id, conclusion)
        ),
    )
    monkeypatch.setattr(
        worker,
        "_mark_native_check_failed",
        lambda check_run_id: failed.append(check_run_id),
    )

    await worker._complete_native_check(
        event,
        handle,
        conclusion="failure",
        message="stranded after prior PATCH failure",
    )

    assert completing == [31]
    assert patched == ["911"]
    assert completed == [(31, "failure")]
    assert failed == []


@pytest.mark.anyio
async def test_finalize_stranded_recovers_after_patch_timeout(
    monkeypatch,
):
    """Acceptance: PATCH timeout → durable failure → reconciler completes check."""
    event = _event()
    handle = _handle(status="failed")
    patch_attempts = 0
    patched: list[str] = []

    monkeypatch.setattr(
        worker,
        "_reconcile_stranded_reviews",
        lambda: (
            StrandedReviewJob(
                id=313,
                repository_id=3,
                payload=event.to_payload(),
                retries_exhausted=True,
            ),
        ),
    )
    monkeypatch.setattr(worker, "_get_native_check_for_job", lambda _job_id: handle)

    async def flaky_then_ok(_event, **kwargs):
        nonlocal patch_attempts
        patch_attempts += 1
        if patch_attempts == 1:
            raise TimeoutError("transient GitHub 5xx")
        patched.append(kwargs["external_id"])

    monkeypatch.setattr(worker, "complete_github_check_run", flaky_then_ok)
    monkeypatch.setattr(worker, "_mark_native_check_completing", lambda *_a: None)
    monkeypatch.setattr(worker, "_mark_native_check_completed", lambda *_a: None)
    monkeypatch.setattr(worker, "_mark_native_check_failed", lambda *_a: None)

    # First stranded pass: PATCH still fails; exception is logged, not fatal.
    assert await worker._finalize_stranded_reviews() == 1
    assert patched == []
    assert patch_attempts == 1

    # Second pass: same reclaimable row; PATCH succeeds → remote terminal.
    assert await worker._finalize_stranded_reviews() == 1
    assert patched == ["911"]
    assert patch_attempts == 2
