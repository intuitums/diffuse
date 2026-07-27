import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import litellm
import psycopg2
import psycopg2.errors
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



def _review_args(**overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        base=None,
        repo=None,
        scm_base_url=None,
        include_untracked=False,
        resume=False,
        diff=False,
        json=False,
        agent=False,
        fail_on_findings=False,
        handler=review_cli._run_review_command,
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _run_review_capturing(monkeypatch, *, tty: bool, **flags) -> tuple[str, str]:
    def fake_review(*, progress=None, **_kwargs):
        if progress is not None:
            progress.stage("retrieving repository context")
            progress.model_step()
            progress.model_step()
        return _result()

    monkeypatch.setattr(review_cli, "run_local_review", fake_review)
    stdout = _Stream(tty=tty)
    stderr = _Stream(tty=tty)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    review_cli._run_review_command(_review_args(**flags))
    return stdout.getvalue(), stderr.getvalue()


def test_every_cli_argument_documents_itself_in_help():
    undocumented: list[str] = []

    def walk(parser: argparse.ArgumentParser, path: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f"{path} {name}")
                continue
            if isinstance(action, argparse._HelpAction):
                continue
            if not action.help:
                undocumented.append(f"{path}: {action.option_strings or action.dest}")

    walk(review_cli._parser(), "diffuse")

    assert undocumented == []


def test_review_help_shows_examples_and_the_exit_code_table(capsys):
    parser = review_cli._parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["review", "--help"])

    assert raised.value.code == review_cli.EXIT_OK
    help_text = capsys.readouterr().out
    assert "diffuse review -b origin/main --diff" in help_text
    assert "diffuse review --json" in help_text
    assert "exit codes:" in help_text
    assert "2  usage error" in help_text
    assert "3  configuration or environment error" in help_text
    assert "--base" in help_text and "Base revision to diff against" in help_text


def test_top_level_help_documents_examples_and_exit_codes(capsys):
    with pytest.raises(SystemExit):
        review_cli._parser().parse_args(["--help"])

    help_text = capsys.readouterr().out
    assert "examples:" in help_text
    assert "exit codes:" in help_text


@pytest.mark.parametrize("flag", ["json", "agent"])
def test_machine_stdout_contract_is_byte_identical_with_and_without_progress(
    monkeypatch,
    flag,
):
    quiet_stdout, quiet_stderr = _run_review_capturing(
        monkeypatch,
        tty=False,
        **{flag: True},
    )
    tty_stdout, tty_stderr = _run_review_capturing(
        monkeypatch,
        tty=True,
        **{flag: True},
    )
    expected = render_json(_result()) if flag == "json" else render_agent(_result())

    assert quiet_stdout.encode() == expected.encode()
    assert tty_stdout.encode() == quiet_stdout.encode()
    assert quiet_stderr == ""
    assert "Diffuse:" in tty_stderr


def test_progress_is_written_only_to_stderr_of_an_interactive_terminal(monkeypatch):
    _, quiet_stderr = _run_review_capturing(monkeypatch, tty=False)
    tty_stdout, tty_stderr = _run_review_capturing(monkeypatch, tty=True)

    assert quiet_stderr == ""
    assert "retrieving repository context" in tty_stderr
    assert "step 2" in tty_stderr
    assert "Diffuse:" not in tty_stdout
    # The line is erased before stdout is written, so nothing is left behind.
    assert tty_stderr.endswith("\r")


def test_review_passes_a_progress_callback_into_the_review_engine(tmp_path, monkeypatch):
    root = _local_git_repository(tmp_path)
    captured: dict[str, object] = {}

    def generate(*_args, **kwargs):
        captured.update(kwargs)
        callback = kwargs["progress_callback"]
        assert callback is not None
        callback()
        callback()
        return _result().report

    monkeypatch.setattr(review_cli, "get_conn", MagicMock)
    monkeypatch.setattr(review_cli, "list_repositories", lambda _conn: [_repository()])
    monkeypatch.setattr(review_cli, "embedding_model", lambda: "embed/test")
    monkeypatch.setattr(review_cli, "embedding_dimensions", lambda: 1536)
    monkeypatch.setattr(review_cli, "active_snapshot_id_for_repository", lambda *_a: 17)
    monkeypatch.setattr(review_cli, "load_active_learned_rules", lambda *_a, **_k: ())
    monkeypatch.setattr(
        review_cli,
        "resolve_cross_repository_context_plan",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        review_cli,
        "retrieve_context_from_plan",
        lambda *_a, **_k: SimpleNamespace(contexts=()),
    )
    monkeypatch.setattr(review_cli, "review_model", lambda: "openai/test")
    monkeypatch.setattr(review_cli, "generate_review", generate)
    stderr = _Stream(tty=True)
    monkeypatch.setattr(sys, "stderr", stderr)

    result = review_cli.run_local_review(
        start=root,
        progress=review_cli._stderr_progress_reporter(),
    )

    assert result is not None
    assert captured["progress_callback"] is not None
    assert "running review model (step 2)" in stderr.getvalue()


def test_base_flag_errors_name_the_flag_the_user_typed():
    with pytest.raises(review_cli.CliUsageError, match=r"--base is not a safe Git branch name"):
        review_cli._valid_base_ref("bad..name")

    # The shared SCM validator keeps its own wording for webhook payloads.
    with pytest.raises(ValueError, match="default_branch"):
        review_cli.validate_branch_name("bad..name")


def test_missing_base_revision_is_a_usage_error_naming_the_flag(tmp_path):
    root = _local_git_repository(tmp_path)

    with pytest.raises(review_cli.CliUsageError, match=r"--base revision does not exist"):
        review_cli.resolve_base_ref(root, _repository(), "no-such-branch")


def test_git_failures_keep_multi_line_output_readable(tmp_path):
    with pytest.raises(ValueError) as raised:
        review_cli._git_bytes(tmp_path, ["rev-parse", "--show-toplevel"])

    rendered = review_cli.format_cli_error(str(raised.value))
    lines = rendered.splitlines()

    assert lines[0].startswith("diffuse: error: git rev-parse --show-toplevel failed")
    assert len(lines) > 1
    assert all(line.startswith("    ") for line in lines[1:])
    assert "usage:" not in rendered


def test_format_cli_error_indents_and_truncates_long_tool_output():
    rendered = review_cli.format_cli_error("head\n" + "\n".join(f"line {n}" for n in range(60)))
    lines = rendered.splitlines()

    assert lines[0] == "diffuse: error: head"
    assert lines[1] == "    line 0"
    assert len(lines) == review_cli.MAX_ERROR_LINES + 1
    assert lines[-1].endswith("more line(s) suppressed")


def test_database_errors_name_the_env_var_and_never_print_the_password(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://diffuse:sup3r-s3cret@127.0.0.1:59999/diffuse",
    )

    message = review_cli.database_error_message(
        psycopg2.OperationalError("connection to server at 127.0.0.1 failed")
    )
    rendered = review_cli.format_cli_error(message)

    assert "sup3r-s3cret" not in rendered
    assert "postgresql://diffuse:***@127.0.0.1:59999/diffuse" in rendered
    assert "DATABASE_URL" in rendered
    assert "docker compose up -d db" in rendered


def test_database_url_default_is_reported_without_claiming_the_env_var_is_set(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    message = review_cli.database_error_message(psycopg2.OperationalError("refused"))

    assert "the built-in default" in message
    assert review_cli.redacted_database_url().count("***") == 1


def test_schema_errors_point_at_the_migration_command(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    message = review_cli.database_error_message(
        psycopg2.errors.UndefinedTable("relation diffuse_repositories does not exist")
    )

    assert "diffuse database migrate" in message


def test_model_errors_name_the_credential_env_var_and_the_model(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-test")

    message = review_cli.model_error_message(
        litellm.exceptions.AuthenticationError(
            message="Incorrect API key provided",
            llm_provider="openai",
            model="openai/gpt-test",
        )
    )

    assert "OPENAI_API_KEY" in message
    assert "openai/gpt-test" in message


def test_model_connection_errors_are_actionable(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-test")

    message = review_cli.model_error_message(
        litellm.exceptions.APIConnectionError(
            message="Connection refused",
            llm_provider="openai",
            model="openai/gpt-test",
        )
    )

    assert "REVIEW_API_BASE" in message
    assert "--resume" in message


def test_secret_values_are_redacted_from_any_diagnostic(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key-000")

    rendered = review_cli.format_cli_error("provider rejected sk-not-a-real-key-000")

    assert "sk-not-a-real-key-000" not in rendered
    assert "***" in rendered


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (review_cli.CliUsageError("--base is bogus"), review_cli.EXIT_USAGE),
        (psycopg2.OperationalError("refused"), review_cli.EXIT_CONFIG),
        (
            litellm.exceptions.AuthenticationError(
                message="bad key",
                llm_provider="openai",
                model="openai/gpt-test",
            ),
            review_cli.EXIT_CONFIG,
        ),
        (ValueError("not enabled in Diffuse"), review_cli.EXIT_CONFIG),
        (RuntimeError("no compatible active index"), review_cli.EXIT_CONFIG),
        (OSError("disk gone"), review_cli.EXIT_CONFIG),
        (KeyError("unexpected"), review_cli.EXIT_INTERNAL),
    ],
)
def test_run_handler_maps_every_failure_to_a_documented_exit_code(
    monkeypatch,
    capsys,
    error,
    expected_code,
):
    monkeypatch.delenv("DIFFUSE_CLI_TRACEBACK", raising=False)

    def handler(_args):
        raise error

    with pytest.raises(SystemExit) as raised:
        review_cli.run_handler(SimpleNamespace(handler=handler))

    captured = capsys.readouterr()
    assert raised.value.code == expected_code
    assert captured.out == ""
    assert captured.err.startswith("diffuse: error: ")
    assert "usage:" not in captured.err
    assert "Traceback" not in captured.err


def test_findings_exit_code_is_distinct_from_failure_exit_codes(monkeypatch):
    monkeypatch.setattr(review_cli, "run_local_review", lambda **_kwargs: _result())
    monkeypatch.setattr(sys, "stdout", _Stream(tty=False))
    monkeypatch.setattr(sys, "stderr", _Stream(tty=False))

    with pytest.raises(SystemExit) as raised:
        review_cli.run_handler(_review_args(fail_on_findings=True))

    assert raised.value.code == review_cli.EXIT_FINDINGS
    assert review_cli.EXIT_FINDINGS not in {
        review_cli.EXIT_USAGE,
        review_cli.EXIT_CONFIG,
        review_cli.EXIT_INTERNAL,
    }


@pytest.mark.parametrize(
    ("argv", "expected_prog"),
    [
        (["diffuse", "review", "--not-a-flag"], "diffuse review"),
        (["diffuse", "repository", "list", "--bogus"], "diffuse repository"),
    ],
)
def test_unknown_flags_exit_2_and_show_the_subcommand_usage(
    monkeypatch,
    capsys,
    argv,
    expected_prog,
):
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as raised:
        review_cli.main()

    stderr = capsys.readouterr().err
    assert raised.value.code == review_cli.EXIT_USAGE
    assert stderr.startswith(f"usage: {expected_prog} ")
    assert f"{expected_prog}: error: unrecognized arguments:" in stderr


def test_traceback_escape_hatch_reraises_for_debugging(monkeypatch):
    monkeypatch.setenv("DIFFUSE_CLI_TRACEBACK", "1")

    def handler(_args):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        review_cli.run_handler(SimpleNamespace(handler=handler))


def test_provider_credentials_are_redacted_from_diagnostics(monkeypatch):
    """Every model provider's key must be redacted, not only OpenAI's.

    `redact_secrets` runs over litellm error strings, which for non-OpenAI
    providers can echo request context. The original hand-maintained list named
    only OpenAI plus a `DIFFUSE_WEBHOOK_SECRET` that does not exist anywhere in
    the codebase, so an Anthropic, Gemini, or OpenRouter key would have been
    printed verbatim.
    """
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-aaaaaaaaaaaaaaaa",
        "GEMINI_API_KEY": "gemini-bbbbbbbbbbbbbbbb",
        "OPENROUTER_API_KEY": "sk-or-cccccccccccccccc",
        "POSTGRES_PASSWORD": "dddddddddddddddd",
        "GITHUB_WEBHOOK_SECRET": "eeeeeeeeeeeeeeee",
        # Not in SECRET_ENV_NAMES: caught by the suffix heuristic instead.
        "SOME_FUTURE_PROVIDER_API_KEY": "ffffffffffffffff",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    leaked = " ".join(secrets.values())
    redacted = review_cli.redact_secrets(f"upstream rejected the request: {leaked}")

    for name, value in secrets.items():
        assert value not in redacted, f"{name} leaked into the diagnostic"
    assert "upstream rejected the request" in redacted


def test_non_secret_configuration_is_not_redacted(monkeypatch):
    """Redacting REVIEW_API_BASE or VERTEXAI_PROJECT would hide useful context."""
    monkeypatch.setenv("REVIEW_API_BASE", "http://ollama.internal:11434")
    monkeypatch.setenv("VERTEXAI_PROJECT", "acme-production")

    text = review_cli.redact_secrets(
        "cannot reach http://ollama.internal:11434 for acme-production"
    )

    assert "http://ollama.internal:11434" in text
    assert "acme-production" in text


def test_a_secret_containing_another_secret_is_fully_redacted(monkeypatch):
    """Longest-first replacement, or the tail of the longer value survives."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefgh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-abcdefghijklmnop")

    redacted = review_cli.redact_secrets("key=sk-abcdefghijklmnop")

    assert "abcdefghijklmnop" not in redacted
    assert redacted == "key=***"


def test_traceback_mode_redacts_the_traceback(monkeypatch, capsys):
    """DIFFUSE_CLI_TRACEBACK is the documented bug-report path, so it must redact.

    Python's default handler prints the exception verbatim, and
    psycopg2.OperationalError routinely embeds the whole connection string.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", "sup3rs3cretvalue")
    review_cli._install_redacting_excepthook()

    try:
        raise RuntimeError("connection failed for user:sup3rs3cretvalue@db")
    except RuntimeError:
        import sys as _sys

        _sys.excepthook(*_sys.exc_info())

    err = capsys.readouterr().err
    assert "sup3rs3cretvalue" not in err
    assert "RuntimeError" in err
    assert "Traceback (most recent call last)" in err
