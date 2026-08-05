"""DEV-289: stranded reconcile must terminalize a check created without a persisted id."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from service.github.check import PublishedCheckRun, ensure_github_check_run
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


@pytest.mark.anyio
async def test_complete_native_check_recovers_null_external_id_before_patch(
    monkeypatch,
):
    """Create-then-crash-before-persist: rediscover by external_key, then conclude."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    event = _event()
    handle = _creating_handle()
    started: list[tuple[int, str, str | None]] = []
    completed: list[tuple[int, str]] = []
    completing: list[int] = []
    failed: list[int] = []
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 91,
                            "external_id": "diffuse-review-run:42",
                            "html_url": "https://example/check/91",
                        }
                    ]
                },
            )
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
            assert request.url.path.endswith("/check-runs/91")
            return httpx.Response(200, json={"id": 91})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    async def fake_ensure(event_arg, **kwargs):
        assert kwargs["external_key"] == handle.external_key
        assert kwargs.get("existing_external_id") is None
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await ensure_github_check_run(
                event_arg,
                external_key=kwargs["external_key"],
                client=client,
            )

    async def fake_complete(event_arg, **kwargs):
        assert kwargs["external_id"] == "91"
        assert kwargs["conclusion"] == "failure"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            from service.github.check import complete_github_check_run

            await complete_github_check_run(
                event_arg,
                external_id=kwargs["external_id"],
                conclusion=kwargs["conclusion"],
                message=kwargs.get("message"),
                client=client,
            )

    monkeypatch.setattr(worker, "ensure_github_check_run", fake_ensure)
    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
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

    await worker._complete_native_check(
        event,
        handle,
        conclusion="failure",
        message="Diffuse could not finish this review.",
    )

    assert started == [(17, "91", "https://example/check/91")]
    assert completing == [17]
    assert completed == [(17, "failure")]
    assert failed == []
    assert len(patches) == 1
    assert patches[0]["status"] == "completed"
    assert patches[0]["conclusion"] == "failure"
    assert patches[0]["output"]["summary"] == "Diffuse could not finish this review."


@pytest.mark.anyio
async def test_complete_native_check_recovers_from_failed_row_with_null_external_id(
    monkeypatch,
):
    """A prior null-id dead-end left status=failed; recovery must still PATCH GitHub."""
    event = _event()
    handle = _creating_handle(status="failed")
    started: list[tuple[int, str, str | None]] = []

    async def fake_ensure(*_a, **_k):
        return PublishedCheckRun("88", "https://example/check/88")

    async def fake_complete(*_a, **kwargs):
        assert kwargs["external_id"] == "88"

    monkeypatch.setattr(worker, "ensure_github_check_run", fake_ensure)
    monkeypatch.setattr(worker, "complete_github_check_run", fake_complete)
    monkeypatch.setattr(
        worker,
        "_mark_native_check_started",
        lambda check_run_id, external_id, external_url: started.append(
            (check_run_id, external_id, external_url)
        ),
    )
    monkeypatch.setattr(worker, "_mark_native_check_completing", lambda *_a: None)
    monkeypatch.setattr(worker, "_mark_native_check_completed", lambda *_a: None)
    monkeypatch.setattr(
        worker,
        "_mark_native_check_failed",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not fail")),
    )

    await worker._complete_native_check(
        event,
        handle,
        conclusion="failure",
        message="stranded",
    )

    assert started == [(17, "88", "https://example/check/88")]


@pytest.mark.anyio
async def test_finalize_stranded_reviews_passes_null_external_id_handle(
    monkeypatch,
):
    event = _event()
    handle = _creating_handle()
    seen: list[CheckRunHandle | None] = []

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

    async def fake_complete(_event, check_handle, **kwargs):
        seen.append(check_handle)
        assert kwargs["conclusion"] == "failure"
        assert kwargs["message"]

    monkeypatch.setattr(worker, "_complete_native_check", fake_complete)

    assert await worker._finalize_stranded_reviews() == 1
    assert seen == [handle]
    assert seen[0] is not None and seen[0].external_id is None


@pytest.mark.anyio
async def test_resolve_native_check_external_id_uses_external_key(monkeypatch):
    event = _event()
    handle = _creating_handle()
    seen: dict[str, object] = {}

    async def fake_ensure(_event, **kwargs):
        seen.update(kwargs)
        return PublishedCheckRun("55", None)

    monkeypatch.setattr(worker, "ensure_github_check_run", fake_ensure)
    monkeypatch.setattr(worker, "_mark_native_check_started", lambda *_a: None)

    resolved = await worker._resolve_native_check_external_id(event, handle)

    assert seen["external_key"] == "diffuse-review-run:42"
    assert seen["existing_external_id"] is None
    assert resolved == replace(
        handle,
        status="in_progress",
        external_id="55",
        external_url=None,
    )
