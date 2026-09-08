"""Stranded reconcile must terminalize a check created without a persisted id."""

from __future__ import annotations

from dataclasses import replace

import pytest
from diffuse import worker
from diffuse.database.check import CheckRunHandle
from diffuse.github.check import PublishedCheckRun
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.workflow import StrandedReviewJob


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 7,
            "web_url": "https://github.com/owner/repo/pull/7",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
        }
    )


def _creating_handle(*, status: str = "creating") -> CheckRunHandle:
    return CheckRunHandle(
        id=17,
        status=status,
        external_key="diffuse-review-run:42",
        external_id=None,
        external_url=None,
        conclusion=None,
    )


@pytest.fixture(autouse=True)
def _count_completion_attempts(monkeypatch):
    """Keep the stranded-recovery tests off a database.

    `_complete_native_check` records an attempt before it resolves a missing
    remote id, because rediscovery is itself a network call that can fail
    forever. These tests care about the resolution, not the bound, so the
    counter always reports the first attempt.
    """

    monkeypatch.setattr(worker, "_begin_native_check_completion", lambda _check_run_id: 1)


def _stub_check_marks(monkeypatch, *, started, completing, completed, failed):
    monkeypatch.setattr(
        worker,
        "_mark_native_check_started",
        lambda check_run_id, external_id, external_url: started.append(
            (check_run_id, external_id, external_url)
        ),
    )
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


@pytest.mark.anyio
async def test_complete_native_check_recovers_null_external_id_before_patch(
    monkeypatch,
):
    """Create-then-crash-before-persist: rediscover by external_key, then conclude."""
    event = _event()
    handle = _creating_handle()
    started: list[tuple[int, str, str | None]] = []
    completed: list[tuple[int, str]] = []
    completing: list[int] = []
    failed: list[int] = []
    patched: list[str] = []
    finds: list[str] = []

    async def fake_find(_event, **kwargs):
        finds.append(kwargs["external_key"])
        return PublishedCheckRun("91", "https://example/check/91")

    async def fake_complete(_event, **kwargs):
        patched.append(kwargs["external_id"])
        assert kwargs["conclusion"] == "failure"

    async def fake_ensure(*_a, **_k):
        raise AssertionError("recovery must not create a check")

    monkeypatch.setattr(worker, "find_github_check_run", fake_find)
    monkeypatch.setattr(worker, "ensure_github_check_run", fake_ensure)
    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    _stub_check_marks(
        monkeypatch,
        started=started,
        completing=completing,
        completed=completed,
        failed=failed,
    )

    await worker._complete_native_check(
        event,
        handle,
        conclusion="failure",
        message="Diffuse could not finish this review.",
    )

    assert finds == ["diffuse-review-run:42"]
    assert started == [(17, "91", "https://example/check/91")]
    assert completing == [17]
    assert completed == [(17, "failure")]
    assert patched == ["91"]
    assert failed == []


@pytest.mark.anyio
async def test_complete_native_check_recovers_from_failed_row_with_null_external_id(
    monkeypatch,
):
    event = _event()
    handle = _creating_handle(status="failed")
    started: list[tuple[int, str, str | None]] = []
    completed: list[tuple[int, str]] = []
    completing: list[int] = []
    failed: list[int] = []

    async def fake_find(*_a, **_k):
        return PublishedCheckRun("88", "https://example/check/88")

    async def fake_complete(*_a, **kwargs):
        assert kwargs["external_id"] == "88"

    monkeypatch.setattr(worker, "find_github_check_run", fake_find)
    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    _stub_check_marks(
        monkeypatch,
        started=started,
        completing=completing,
        completed=completed,
        failed=failed,
    )

    await worker._complete_native_check(
        event,
        handle,
        conclusion="failure",
        message="stranded",
    )

    assert started == [(17, "88", "https://example/check/88")]
    assert failed == []


@pytest.mark.anyio
async def test_complete_native_check_marks_failed_when_remote_check_missing(
    monkeypatch,
):
    event = _event()
    handle = _creating_handle()
    failed: list[int] = []
    posts = 0

    async def fake_find(*_a, **_k):
        return None

    async def fake_ensure(*_a, **_k):
        nonlocal posts
        posts += 1
        raise AssertionError("must not create")

    monkeypatch.setattr(worker, "find_github_check_run", fake_find)
    monkeypatch.setattr(worker, "ensure_github_check_run", fake_ensure)
    monkeypatch.setattr(
        worker,
        "_mark_native_check_failed",
        lambda check_run_id: failed.append(check_run_id),
    )

    with pytest.raises(RuntimeError, match="No GitHub check run found"):
        await worker._complete_native_check(
            event,
            handle,
            conclusion="failure",
            message="stranded",
        )

    assert failed == [17]
    assert posts == 0


@pytest.mark.anyio
async def test_finalize_stranded_reviews_completes_unpersisted_remote_check(
    monkeypatch,
):
    event = _event()
    handle = _creating_handle()
    patched: list[str] = []

    monkeypatch.setattr(
        worker,
        "_reconcile_stranded_reviews",
        lambda: (
            StrandedReviewJob(
                id=99,
                repository_id=3,
                payload=event.to_payload(),
                retries_exhausted=True,
            ),
        ),
    )
    monkeypatch.setattr(worker, "_get_native_check_for_job", lambda _job_id: handle)

    async def fake_find(*_a, **_k):
        return PublishedCheckRun("91", "https://example/check/91")

    async def fake_complete(*_a, **kwargs):
        patched.append(kwargs["external_id"])

    monkeypatch.setattr(worker, "find_github_check_run", fake_find)
    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    monkeypatch.setattr(worker, "_mark_native_check_started", lambda *_a: None)
    monkeypatch.setattr(worker, "_mark_native_check_completing", lambda *_a: None)
    monkeypatch.setattr(worker, "_mark_native_check_completed", lambda *_a: None)

    assert await worker._finalize_stranded_reviews() == 1
    assert patched == ["91"]


@pytest.mark.anyio
async def test_resolve_native_check_external_id_uses_external_key(monkeypatch):
    event = _event()
    handle = _creating_handle()
    seen: dict[str, object] = {}

    async def fake_find(_event, **kwargs):
        seen.update(kwargs)
        return PublishedCheckRun("55", None)

    monkeypatch.setattr(worker, "find_github_check_run", fake_find)
    monkeypatch.setattr(worker, "_mark_native_check_started", lambda *_a: None)

    resolved = await worker._resolve_native_check_external_id(event, handle)

    assert seen["external_key"] == "diffuse-review-run:42"
    assert resolved == replace(
        handle,
        status="in_progress",
        external_id="55",
        external_url=None,
    )
