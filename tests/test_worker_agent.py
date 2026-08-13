"""Worker-only invariants for the CLI-native review path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from repository_policy.models import PolicyLayer, RepositoryConfig, RepositoryPolicySnapshot
from repository_policy.resolve import resolve_review_policy
from service.hosted import worker
from service.hosted.workflow import WorkflowJob
from service.review.agent_client import NativeRunnerError

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


def _context_plan():
    return SimpleNamespace(fingerprint="1" * 64)


def _session(**overrides):
    values = dict(
        session_id="candidate-session-1",
        runtime="codex",
        repository_id=7,
        pull_request_id=11,
        snapshot_id=13,
        base_sha="a" * 40,
        head_sha="b" * 40,
        capability="capability-token",
        capability_id="capability-1",
        context_plan_fingerprint="1" * 64,
        role=worker.AgentInvestigationRole.CANDIDATE,
        turn_budget=24,
        timeout_seconds=600,
        max_result_bytes=256_000,
        source_artifact=SimpleNamespace(digest="2" * 64, manifest_digest="3" * 64),
        expires_at=SimpleNamespace(),
        input_result=None,
        input_result_digest=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (ValueError("agent_auth_required:codex"), "auth_required"),
        (ValueError("agent_configuration_error:claude"), "configuration_error"),
        (NativeRunnerError("rate limited", code="rate_limited"), "rate_limited"),
        (RuntimeError("unexpected"), "runner_execution_failed"),
    ],
)
def test_agent_investigation_failure_preserves_its_terminal_code(error, error_code):
    assert worker._agent_investigation_error_code(error) == error_code


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


def test_worker_mints_the_verifier_only_after_candidate_output_is_accepted(monkeypatch):
    candidate = _session()
    verifier = _session(
        session_id="verifier-session-1",
        runtime="claude",
        capability_id="capability-2",
        role=worker.AgentInvestigationRole.VERIFIER,
    )
    created: list[str] = []
    persisted = []

    monkeypatch.setattr(worker, "get_conn", lambda: _Connection())
    monkeypatch.setattr(worker, "_heartbeat_and_check_current", lambda *_args: True)
    monkeypatch.setattr(worker, "resolve_review_agent", lambda _runtime: object())
    monkeypatch.setattr(
        worker,
        "_create_native_agent_investigation",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        worker,
        "_create_native_verifier_investigation",
        lambda *_args, **_kwargs: created.append("verifier") or verifier,
    )
    def generate_review_stub(*_args, **kwargs):
        kwargs["request"].native_verifier_factory(
            "claude",
            {"session_id": candidate.session_id, "runtime": candidate.runtime},
            "4" * 64,
        )
        return SimpleNamespace(
            summary="ok",
            risk_score=0,
            findings=[],
            diff_file_count=1,
            reviewed_file_count=1,
            ignored_file_count=0,
            inline_comments_enabled=True,
            publication_enabled=True,
            skip_reason=None,
            context_chunk_count=0,
            prompt_tokens=1,
            completion_tokens=1,
            verifier_prompt_tokens=1,
            verifier_completion_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            confidence_score=5,
        )

    monkeypatch.setattr(worker, "generate_review", generate_review_stub)
    monkeypatch.setattr(
        worker,
        "persist_review_report",
        lambda _conn, review_run_id, report, **kwargs: persisted.append(
            (review_run_id, report, kwargs)
        ),
    )

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
        base_sha="a" * 40,
        head_sha="b" * 40,
        context_plan=_context_plan(),
    )

    assert created == ["verifier"]
    assert persisted[0][0] == 41


def test_native_session_is_retired_when_generation_fails(monkeypatch):
    """A failed worker attempt cannot leave its still-valid token dispatched."""

    session = SimpleNamespace(
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability="capability-token",
        capability_id="capability-1",
    )
    retired: list[tuple[object, ...]] = []
    failed: list[int] = []
    cancelled: list[str] = []

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NativeRunnerError("codex runner was rate limited", code="rate_limited")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_best_effort_cancel_host_investigation",
        lambda investigation: cancelled.append(investigation.session_id),
    )
    monkeypatch.setattr(
        worker,
        "abandon_agent_investigation",
        lambda _conn, **kwargs: retired.append(
            (
                kwargs["session_id"],
                kwargs["runtime"],
                kwargs["capability_id"],
                kwargs["status"],
                kwargs["error_code"],
            )
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "mark_review_failed",
        lambda _conn, review_run_id: failed.append(review_run_id),
    )

    with pytest.raises(NativeRunnerError, match="rate limited"):
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
            base_sha="a" * 40,
            head_sha="b" * 40,
            context_plan=_context_plan(),
        )

    assert cancelled == ["a32b1c5d-3c15-4462-a9fe-f191775b3459"]
    assert retired == [
        (
            "a32b1c5d-3c15-4462-a9fe-f191775b3459",
            "codex",
            "capability-1",
            "failed",
            "rate_limited",
        )
    ]
    assert failed == [41]


def test_native_verifier_reuses_the_candidate_artifact_and_opposite_runtime(monkeypatch):
    created = []
    candidate = _session()
    context_plan = _context_plan()

    monkeypatch.setattr(
        worker,
        "_create_native_agent_investigation",
        lambda job, **kwargs: created.append((job, kwargs)) or _session(
            runtime=kwargs["runtime"],
            role=kwargs["role"],
            source_artifact=kwargs["source_artifact"],
            input_result=kwargs["input_result"],
            input_result_digest=kwargs["input_result_digest"],
        ),
    )

    verifier = worker._create_native_verifier_investigation(
        _job(),
        candidate_session=candidate,
        candidate_result={"summary": "candidate"},
        candidate_result_digest="4" * 64,
        context_plan=context_plan,
        progress_callback=lambda: None,
    )

    _job_seen, kwargs = created[0]
    assert kwargs["runtime"] == "claude"
    assert kwargs["role"] is worker.AgentInvestigationRole.VERIFIER
    assert kwargs["source_artifact"] is candidate.source_artifact
    assert kwargs["input_result"] == {"summary": "candidate"}
    assert kwargs["input_result_digest"] == "4" * 64
    assert verifier.runtime == "claude"
    assert verifier.input_result_digest == "4" * 64


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
        lambda *_args, **_kwargs: SimpleNamespace(digest="2" * 64, manifest_digest="3" * 64),
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
            base_sha="a" * 40,
            head_sha="b" * 40,
            context_plan=_context_plan(),
            progress_callback=report_progress,
        )

    assert progress_calls == 2
