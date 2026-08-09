"""Worker-only invariants for the CLI-native review path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from repository_policy.models import PolicyLayer, RepositoryConfig, RepositoryPolicySnapshot
from repository_policy.resolve import resolve_review_policy
from service.hosted import worker
from service.hosted.workflow import WorkflowJob

DIFF = """\\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = False
+new = True
"""


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None


def _disabled_policy():
    return resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {"version": 1, "review": {"enabled": False}}
                    ),
                ),
            )
        ),
        {"app.py"},
    )


def _enabled_policy():
    return resolve_review_policy(RepositoryPolicySnapshot(), {"app.py"})


def _job() -> WorkflowJob:
    return WorkflowJob(
        id=71,
        repository_id=7,
        pull_request_id=11,
        job_type="review_pull_request",
        scope_key="github:example/repository:11",
        base_revision="a" * 40,
        revision="b" * 40,
        payload={},
        attempt_count=1,
        max_attempts=5,
    )


def test_native_review_skips_before_artifact_or_session_when_policy_disables_all_files(monkeypatch):
    persisted = []
    monkeypatch.setattr(worker, "hosted_review_agent_name", lambda: "codex")
    monkeypatch.setattr(worker, "_heartbeat_and_check_current", lambda *_args: True)
    monkeypatch.setattr(worker, "get_conn", lambda: _Connection())
    monkeypatch.setattr(
        worker,
        "persist_review_report",
        lambda _conn, review_run_id, report, **kwargs: persisted.append(
            (review_run_id, report, kwargs)
        ),
    )
    monkeypatch.setattr(
        worker,
        "_create_native_agent_investigation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    worker._generate_and_persist_review(
        _job(),
        review_run_id=41,
        diff_text=DIFF,
        contexts=[],
        worker_id="worker-1",
        policy=_disabled_policy(),
        touched_paths=frozenset(),
        runtime_name="codex",
    )

    assert len(persisted) == 1
    review_run_id, report, kwargs = persisted[0]
    assert review_run_id == 41
    assert report.skip_reason == "all_files_disabled"
    assert not report.publication_enabled
    assert kwargs == {"touched_paths": frozenset(), "path_aliases": None}


def test_native_session_is_retired_when_generation_fails(monkeypatch):
    """A failed worker attempt cannot leave its still-valid token dispatched."""

    session = SimpleNamespace(
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability_id="capability-1",
    )
    retired: list[tuple[object, ...]] = []
    failed: list[int] = []

    monkeypatch.setattr(worker, "get_conn", lambda: _Connection())
    monkeypatch.setattr(worker, "resolve_review_agent", lambda _runtime: object())
    monkeypatch.setattr(
        worker,
        "_create_native_agent_investigation",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(
        worker,
        "generate_review",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runner timed out")),
    )
    monkeypatch.setattr(
        worker,
        "abandon_agent_investigation",
        lambda _conn, **kwargs: retired.append(
            (kwargs["session_id"], kwargs["runtime"], kwargs["capability_id"], kwargs["status"])
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "mark_review_failed",
        lambda _conn, review_run_id: failed.append(review_run_id),
    )

    with pytest.raises(RuntimeError, match="runner timed out"):
        worker._generate_and_persist_review(
            _job(),
            review_run_id=41,
            diff_text=DIFF,
            contexts=[],
            worker_id="worker-1",
            policy=_enabled_policy(),
            touched_paths=frozenset(),
            runtime_name="codex",
            snapshot_id=13,
            head_sha="b" * 40,
        )

    assert retired == [
        ("a32b1c5d-3c15-4462-a9fe-f191775b3459", "codex", "capability-1", "failed")
    ]
    assert failed == [41]


def test_native_session_is_not_persisted_after_artifact_supersession(monkeypatch):
    """A superseded checkout may waste local work but cannot dispatch a runner."""

    progress_calls = 0

    def report_progress():
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls == 2:
            raise worker.ReviewSupersededError("newer revision")

    monkeypatch.setattr(
        worker,
        "_build_native_workspace_artifact",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        worker,
        "create_agent_investigation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not persist a superseded session")
        ),
    )

    with pytest.raises(worker.ReviewSupersededError, match="newer revision"):
        worker._create_native_agent_investigation(
            _job(),
            runtime="codex",
            snapshot_id=13,
            head_sha="b" * 40,
            progress_callback=report_progress,
        )

    assert progress_calls == 2
