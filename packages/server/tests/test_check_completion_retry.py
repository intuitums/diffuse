"""DEV-313: transient check completion failure must stay reclaimable.

The pairing these tests exist to pin: a failure is *recorded* on the row and the
row is still *retried*. Those used to be the same decision -- writing `failed`
was what removed a check from the stranded sweep -- which is why one PATCH
timeout left a required check `in_progress` on GitHub permanently. The exit is
now `completion_attempts`, so both properties can hold at once.
"""

from __future__ import annotations

import pytest
from diffuse import worker
from diffuse.database.check import MAX_COMPLETION_ATTEMPTS, CheckRunHandle
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.workflow import NonRetryableError, StrandedReviewJob


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


def _handle(*, status: str = "in_progress", external_id: str | None = "911") -> CheckRunHandle:
    return CheckRunHandle(
        id=31,
        status=status,
        external_key="diffuse-review-run:77",
        external_id=external_id,
        external_url="https://example/check/911",
        conclusion=None,
    )


class _Store:
    """The durable calls `_complete_native_check` makes, recorded in order.

    Substituting the whole set keeps these tests off a database while still
    letting them assert the thing that matters: which durable transition ran,
    and how many times.
    """

    def __init__(self, *, attempts_before: int = 0) -> None:
        self.attempts = attempts_before
        self.completing: list[int] = []
        self.completed: list[tuple[int, str]] = []
        self.failed: list[int] = []
        self.exhausted: list[int] = []

    def install(self, monkeypatch) -> _Store:
        monkeypatch.setattr(worker, "_begin_native_check_completion", self._begin)
        monkeypatch.setattr(worker, "_mark_native_check_completing", self.completing.append)
        monkeypatch.setattr(
            worker,
            "_mark_native_check_completed",
            lambda check_run_id, conclusion: self.completed.append((check_run_id, conclusion)),
        )
        monkeypatch.setattr(worker, "_mark_native_check_failed", self.failed.append)
        monkeypatch.setattr(
            worker,
            "_mark_native_check_completion_exhausted",
            self.exhausted.append,
        )
        return self

    def _begin(self, _check_run_id: int) -> int:
        self.attempts += 1
        return self.attempts


@pytest.mark.anyio
async def test_patch_failure_is_recorded_and_still_reclaimable(monkeypatch):
    """The durable failure and the retry are no longer mutually exclusive."""
    store = _Store().install(monkeypatch)

    async def fake_complete(*_a, **_k):
        raise TimeoutError("GitHub check PATCH timed out")

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)

    with pytest.raises(TimeoutError, match="timed out"):
        await worker._complete_native_check(
            _event(),
            _handle(),
            conclusion="failure",
            message="Diffuse could not finish this review.",
        )

    assert store.completing == [31]
    # Recorded, so `check_run_publication_failed` still reaches the row ...
    assert store.failed == [31]
    # ... and not exhausted, so the sweep will pick this row up again.
    assert store.exhausted == []
    assert store.completed == []


@pytest.mark.anyio
async def test_complete_native_check_retries_after_prior_failed_status(monkeypatch):
    """Store already allows failed→completing; completer must use that path."""
    store = _Store().install(monkeypatch)
    patched: list[str] = []

    async def fake_complete(_event, **kwargs):
        patched.append(kwargs["external_id"])
        assert kwargs["conclusion"] == "failure"

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)

    await worker._complete_native_check(
        _event(),
        _handle(status="failed"),
        conclusion="failure",
        message="stranded after prior PATCH failure",
    )

    assert store.completing == [31]
    assert patched == ["911"]
    assert store.completed == [(31, "failure")]
    assert store.failed == []


@pytest.mark.anyio
async def test_unresolvable_remote_id_is_recorded_without_becoming_terminal(monkeypatch):
    """DEV-289 recovery is a network call, so its failure is transient too.

    The row is marked `failed` with a null `external_id`, which the store
    deliberately allows to recover via `mark_check_run_started`. Excluding that
    shape from the sweep would make the recovery path unreachable and reproduce
    DEV-313 one step earlier in the sequence.
    """

    store = _Store().install(monkeypatch)

    async def missing(*_a, **_k):
        raise RuntimeError("No GitHub check run found")

    monkeypatch.setattr(worker, "find_github_check_run", missing)

    with pytest.raises(RuntimeError, match="No GitHub check run"):
        await worker._complete_native_check(
            _event(),
            _handle(status="failed", external_id=None),
            conclusion="failure",
        )

    assert store.failed == [31]
    assert store.exhausted == []
    assert store.completing == []


@pytest.mark.anyio
async def test_completion_stops_after_the_attempt_cap(monkeypatch):
    """Without an exit the sweep starves: it is ordered oldest-first and limited."""
    store = _Store(attempts_before=MAX_COMPLETION_ATTEMPTS).install(monkeypatch)
    patched: list[str] = []

    async def fake_complete(_event, **kwargs):
        patched.append(kwargs["external_id"])

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)

    with pytest.raises(NonRetryableError, match="Gave up completing check run"):
        await worker._complete_native_check(
            _event(),
            _handle(),
            conclusion="failure",
        )

    assert store.exhausted == [31]
    # The point of the cap: no further provider call is made for this row.
    assert patched == []
    assert store.completing == []


@pytest.mark.anyio
async def test_completion_race_with_another_worker_is_a_no_op(monkeypatch):
    """A null attempt count means the row reached `completed` underneath us."""
    store = _Store().install(monkeypatch)
    monkeypatch.setattr(worker, "_begin_native_check_completion", lambda _id: None)

    async def fake_complete(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("completed check must not be PATCHed again")

    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)

    await worker._complete_native_check(_event(), _handle(), conclusion="failure")

    assert store.completing == []
    assert store.failed == []
    assert store.exhausted == []


@pytest.mark.anyio
async def test_finalize_stranded_recovers_after_patch_timeout(monkeypatch):
    """Acceptance: PATCH timeout → durable failure → reconciler completes check."""
    event = _event()
    handle = _handle(status="failed")
    store = _Store().install(monkeypatch)
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

    # First stranded pass: PATCH still fails; exception is logged, not fatal.
    assert await worker._finalize_stranded_reviews() == 1
    assert patched == []
    assert patch_attempts == 1
    assert store.failed == [31]

    # Second pass: same reclaimable row; PATCH succeeds → remote terminal.
    assert await worker._finalize_stranded_reviews() == 1
    assert patched == ["911"]
    assert patch_attempts == 2
    assert store.completed == [(31, "failure")]
