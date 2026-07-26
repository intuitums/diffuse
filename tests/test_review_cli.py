import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from service import cluster_cli, learning_cli, repository_cli, review_cli
from service.repositories import RegisteredRepository
from service.review_cli import (
    CliReviewState,
    LocalDiff,
    LocalReviewResult,
    _load_state,
    _remote_parts,
    _terminal_text,
    _write_state,
    collect_local_diff,
    render_agent,
    render_human,
    render_json,
    select_registered_repository,
)
from service.review_models import Category, ReviewFinding, ReviewReport, Severity


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(**overrides) -> RegisteredRepository:
    values = {
        "id": 7,
        "scm_provider": "github",
        "scm_base_url": "https://github.com",
        "full_name": "owner/repo",
        "default_branch": "main",
        "clone_url": "https://github.com/owner/repo.git",
        "enabled": True,
        "mirror_state": "ready",
        "last_fetched_sha": "a" * 40,
        "last_error_code": None,
    }
    values.update(overrides)
    return RegisteredRepository(**values)


def _local_git_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Diffuse Test")
    _git(root, "config", "user.email", "diffuse@example.invalid")
    (root / "app.py").write_text("value = 1\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "base")
    _git(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    _git(root, "switch", "-c", "feature")
    (root / "app.py").write_text("value = 2\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "change")
    (root / "notes.txt").write_text("untracked context\n")
    return root


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:owner/repo.git", ("github.com", "owner/repo")),
        (
            "ssh://git@gitlab.example.com/group/sub/repo.git",
            ("gitlab.example.com", "group/sub/repo"),
        ),
        (
            "https://github.example.com/owner/repo.git",
            ("github.example.com", "owner/repo"),
        ),
    ],
)
def test_remote_parts_support_common_cloud_and_self_hosted_urls(remote, expected):
    assert _remote_parts(remote) == expected


def test_repository_selection_matches_enabled_origin_and_rejects_ambiguity(tmp_path):
    root = _local_git_repository(tmp_path)
    selected = select_registered_repository(
        root,
        [
            _repository(),
            _repository(
                id=8,
                full_name="other/repo",
                clone_url="https://github.com/other/repo.git",
            ),
        ],
    )
    assert selected.id == 7

    _git(root, "remote", "remove", "origin")
    with pytest.raises(ValueError, match="ambiguous"):
        select_registered_repository(
            root,
            [
                _repository(),
                _repository(
                    id=8,
                    scm_base_url="https://github.enterprise.example",
                    clone_url="https://github.enterprise.example/owner/repo.git",
                ),
            ],
            requested_name="owner/repo",
        )


def test_local_diff_uses_merge_base_and_requires_opt_in_for_untracked_files(tmp_path):
    root = _local_git_repository(tmp_path)

    tracked_only = collect_local_diff(root, _repository())
    with_untracked = collect_local_diff(
        root,
        _repository(),
        include_untracked=True,
    )

    assert tracked_only.base_ref == "main"
    assert tracked_only.head_sha == _git(root, "rev-parse", "HEAD")
    assert "-value = 1" in tracked_only.diff_text
    assert "+value = 2" in tracked_only.diff_text
    assert "notes.txt" not in tracked_only.diff_text
    assert tracked_only.untracked_paths == ("notes.txt",)
    assert not tracked_only.included_untracked
    assert "notes.txt" in with_untracked.diff_text
    assert "+untracked context" in with_untracked.diff_text
    assert with_untracked.included_untracked


def test_local_diff_rejects_oversized_untracked_input_before_rendering(
    tmp_path,
    monkeypatch,
):
    root = _local_git_repository(tmp_path)
    (root / "notes.txt").write_text("x" * 2048)
    monkeypatch.setattr(review_cli, "MAX_LOCAL_DIFF_BYTES", 1024)

    with pytest.raises(ValueError, match="Untracked review input exceeds"):
        collect_local_diff(
            root,
            _repository(),
            include_untracked=True,
        )


def _result() -> LocalReviewResult:
    diff_text = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-return trusted\n"
        "+return user_input\n"
    )
    finding = ReviewFinding(
        fingerprint="f" * 64,
        title="Validate input",
        body="The new return exposes untrusted input.\x1b[31m",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.92,
        file_path="app.py",
        line=1,
        side="RIGHT",
        evidence="The added line returns user_input directly.",
        suggested_fix="Validate user_input before returning it.",
    )
    return LocalReviewResult(
        repository=_repository(),
        local_diff=LocalDiff(
            root=Path("/tmp/repo"),
            base_ref="main",
            merge_base_sha="b" * 40,
            head_sha="a" * 40,
            diff_text=diff_text,
            untracked_paths=(),
        ),
        snapshot_id=17,
        review_model_name="test/model",
        review_verifier_model_name="test/verifier",
        prompt_version="native-review-test",
        report=ReviewReport(
            summary="One issue.",
            risk_score=7,
            confidence_score=2,
            findings=[finding],
            diff_file_count=1,
            reviewed_file_count=1,
            context_chunk_count=2,
            prompt_tokens=10,
            completion_tokens=3,
        ),
    )


def test_cli_renderers_support_human_inline_json_and_agent_output():
    result = _result()

    human = render_human(result, include_diff=True)
    agent = render_agent(result)
    machine = json.loads(render_json(result, include_diff=True))

    assert "Confidence 2/5" in human
    assert "Relevant diff:" in human
    assert "\x1b" not in human
    assert "DIFFUSE REVIEW" in agent
    assert "Suggested fix:" in agent
    assert "\x1b" not in agent
    assert machine["schema_version"] == "diffuse-cli-review-v1"
    assert machine["index_snapshot_id"] == 17
    assert machine["review_model"] == "test/model"
    assert machine["report"]["findings"][0]["severity"] == "high"
    assert "return user_input" in machine["diff_snippets"]["f" * 64]


def test_terminal_output_removes_control_characters():
    assert _terminal_text("safe\x00\x1b[31m\ntext\tvalue") == "safe\ntext\tvalue"


def test_unified_cli_routes_operator_commands_without_breaking_legacy_parsers():
    parser = review_cli._parser()

    repository = parser.parse_args(["repository", "list"])
    cluster = parser.parse_args(["cluster", "list"])
    learning = parser.parse_args(["learning", "show", "7", "11"])

    assert repository.command == "repository"
    assert repository.repository_command == "list"
    assert repository.handler is repository_cli._list_registered_repositories
    assert cluster.command == "cluster"
    assert cluster.cluster_command == "list"
    assert cluster.handler is cluster_cli._list
    assert learning.command == "learning"
    assert learning.learning_command == "show"
    assert learning.repository_id == 7
    assert learning.rule_id == 11
    assert learning.handler is learning_cli._show
    assert repository_cli._parser().parse_args(["list"]).handler is (
        repository_cli._list_registered_repositories
    )
    assert cluster_cli._parser().parse_args(["list"]).handler is cluster_cli._list
    assert learning_cli._parser().parse_args(["show", "7", "11"]).handler is (
        learning_cli._show
    )


def test_unified_cli_dispatches_repository_handler(monkeypatch, capsys):
    def list_repositories(_args):
        print("repository-dispatch-ok")

    monkeypatch.setattr(
        repository_cli,
        "_list_registered_repositories",
        list_repositories,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["diffuse", "repository", "list"],
    )

    review_cli.main()

    assert capsys.readouterr().out == "repository-dispatch-ok\n"


def test_cli_review_state_is_atomic_validated_and_resume_ready(tmp_path):
    root = _local_git_repository(tmp_path)
    state = CliReviewState(
        status="failed",
        repository_id=7,
        repository_full_name="owner/repo",
        base_ref="main",
        diff_fingerprint="d" * 64,
        include_untracked=False,
        index_snapshot_id=17,
        policy_fingerprint="e" * 64,
        review_model="openai/test",
        review_verifier_model="anthropic/test",
        prompt_version="native-review-test",
        attempt_count=2,
        error_code="review_failed",
    )

    _write_state(root, state)
    loaded = _load_state(root)

    assert loaded == state
    state_path = root / ".git" / "diffuse" / "cli-review.json"
    assert state_path.is_file()
    assert not list(state_path.parent.glob("*.tmp"))


def _configure_local_review(monkeypatch, *, repository) -> None:
    """Stub the database, retrieval, and model configuration a local review uses."""

    monkeypatch.setattr(review_cli, "get_conn", MagicMock)
    monkeypatch.setattr(
        review_cli,
        "list_repositories",
        lambda _conn: [repository],
    )
    monkeypatch.setattr(review_cli, "embedding_model", lambda: "embed/test")
    monkeypatch.setattr(review_cli, "embedding_dimensions", lambda: 1536)
    monkeypatch.setattr(
        review_cli,
        "active_snapshot_id_for_repository",
        lambda *_args: 17,
    )
    monkeypatch.setattr(
        review_cli,
        "load_active_learned_rules",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        review_cli,
        "resolve_cross_repository_context_plan",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        review_cli,
        "retrieve_context_from_plan",
        lambda *_args, **_kwargs: SimpleNamespace(contexts=()),
    )
    monkeypatch.setattr(review_cli, "review_model", lambda: "openai/test")
    monkeypatch.setattr(
        review_cli,
        "review_verifier_model",
        lambda: "anthropic/test",
    )


def test_failed_local_review_resumes_only_the_same_immutable_inputs(
    tmp_path,
    monkeypatch,
):
    root = _local_git_repository(tmp_path)
    generated = 0
    generate_calls: list[dict[str, object]] = []

    def generate(*_args, **kwargs):
        nonlocal generated
        generated += 1
        generate_calls.append(kwargs)
        if generated == 1:
            raise RuntimeError("transient model failure")
        return ReviewReport(
            summary="No issues.",
            risk_score=0,
            findings=[],
            diff_file_count=1,
            reviewed_file_count=1,
            context_chunk_count=0,
            prompt_tokens=4,
            completion_tokens=1,
        )

    _configure_local_review(monkeypatch, repository=_repository())
    monkeypatch.setattr(review_cli, "generate_review", generate)

    with pytest.raises(RuntimeError, match="transient"):
        review_cli.run_local_review(start=root)
    failed = _load_state(root)
    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.review_verifier_model == "anthropic/test"

    result = review_cli.run_local_review(start=root, resume=True)
    completed = _load_state(root)

    assert result is not None
    assert result.snapshot_id == 17
    assert result.review_model_name == "openai/test"
    assert result.review_verifier_model_name == "anthropic/test"
    assert result.prompt_version == review_cli.PROMPT_VERSION
    assert completed.status == "completed"
    assert completed.attempt_count == 2
    assert generated == 2
    # Generation must use the pinned pair, not whatever the environment says at
    # the moment the resumed attempt runs.
    assert generate_calls[-1]["candidate_model"] == "openai/test"
    assert generate_calls[-1]["verifier_model"] == "anthropic/test"


def test_a_changed_verifier_model_refuses_to_resume(tmp_path, monkeypatch):
    """The verifier is part of the run's identity, like the candidate model.

    Regression: only `REVIEW_MODEL` was persisted, so an operator who changed
    `REVIEW_VERIFIER_MODEL` after a failed attempt got a silent retry with a
    different verifier and stored state that no longer described the run.
    """

    root = _local_git_repository(tmp_path)
    _configure_local_review(monkeypatch, repository=_repository())
    monkeypatch.setattr(
        review_cli,
        "generate_review",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("transient model failure")
        ),
    )

    with pytest.raises(RuntimeError, match="transient"):
        review_cli.run_local_review(start=root)

    monkeypatch.setattr(
        review_cli,
        "review_verifier_model",
        lambda: "google/other",
    )

    with pytest.raises(ValueError, match="Review inputs changed"):
        review_cli.run_local_review(start=root, resume=True)
