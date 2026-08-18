"""Contract coverage for the offline-testable agent investigation primitive."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from diffuse_host import investigation
from diffuse_host.claude import build_argv, parse_envelope
from diffuse_host.context_bridge import MAX_SEARCH_LIMIT, McpBridge, write_mcp_config
from diffuse_host.environment import CREDENTIAL_ENVIRONMENT
from diffuse_host.errors import (
    AgentInvestigationCoverageCaveat,
    AgentInvestigationError,
    AgentInvestigationExecutionError,
    AgentInvestigationMcpError,
    AgentInvestigationOutputError,
    AgentInvestigationRateLimited,
    AgentInvestigationTerminalError,
    AgentInvestigationTimeout,
)
from diffuse_host.replay import SessionTranscript, record, replay
from diffuse_host.runtime import (
    CLAUDE_CODE,
    CONTAINER_COMPARTMENT_PROFILE,
    assert_compartment,
    write_sandbox_settings,
)
from diffuse_protocol.profiles import REVIEW, SessionProfile
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str


def _envelope(result: object, **extra: object) -> str:
    return json.dumps({"result": result, "usage": {"input_tokens": 3, "output_tokens": 5}, **extra})


@pytest.fixture
def owned_policy(monkeypatch, tmp_path) -> Path:
    """A Diffuse-owned agent home with the current sandbox policy written.

    `run_structured` refuses to start without one, so every investigation test needs
    this. Pointing `DIFFUSE_REVIEW_AGENT_HOME` at a disposable directory also keeps the
    suite from rewriting the policy a developer's own reviews run under.
    """

    home = tmp_path / "agent-home"
    monkeypatch.setenv("DIFFUSE_REVIEW_AGENT_HOME", str(home))
    write_sandbox_settings(CLAUDE_CODE)
    return home


@pytest.fixture
def executable(monkeypatch, tmp_path, owned_policy) -> Path:
    path = tmp_path / "claude"
    path.write_text("")
    monkeypatch.setattr(investigation, "resolve_executable", lambda _cli: path)
    return path


def test_a_session_refuses_to_run_without_the_owned_sandbox_policy(
    monkeypatch, tmp_path
):
    """No policy on disk means no `failIfUnavailable`, which means no sandbox."""

    monkeypatch.setenv("DIFFUSE_REVIEW_AGENT_HOME", str(tmp_path / "never-logged-in"))
    monkeypatch.setattr(investigation, "resolve_executable", lambda _cli: tmp_path / "claude")

    def runner(*_args):  # pragma: no cover - must not be reached
        raise AssertionError("a investigation must not spawn without its sandbox policy")

    with pytest.raises(AgentInvestigationTerminalError, match="write-policy"):
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )


def test_the_session_settings_narrow_reads_to_the_worktree(tmp_path, owned_policy):
    """The per-review `allowRead` is the part that cannot live in the config dir."""

    worktree = tmp_path / "worktree"
    path = investigation.write_session_settings(
        tmp_path / "settings.json",
        cli=CLAUDE_CODE,
        worktree=worktree,
    )
    sandbox = json.loads(path.read_text())["sandbox"]

    assert sandbox["filesystem"]["allowRead"] == [str(worktree)]
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


def test_compartment_session_renders_its_ephemeral_profile_without_local_policy(
    monkeypatch, tmp_path
):
    """The credential volume is not also required to hold a local policy."""

    monkeypatch.setenv("DIFFUSE_REVIEW_AGENT_HOME", str(tmp_path / "agent-home"))
    executable = tmp_path / "claude"
    executable.write_text("")
    monkeypatch.setattr(investigation, "resolve_executable", lambda _cli: executable)
    observed: dict[str, object] = {}
    assertion = assert_compartment(CONTAINER_COMPARTMENT_PROFILE, lambda: None)

    def runner(argv, environment, _cwd, _timeout):
        settings = Path(argv[argv.index("--settings") + 1])
        observed["settings"] = json.loads(settings.read_text())
        observed["environment"] = environment
        return investigation.SessionRun(0, _envelope('{"answer":"ok"}'), "")

    value, _, _ = investigation.run_structured(
        CLAUDE_CODE,
        Answer,
        system_prompt="system",
        user_prompt="user",
        workspace=tmp_path,
        tools=None,
        profile=REVIEW,
        sandbox_profile=CONTAINER_COMPARTMENT_PROFILE,
        compartment=assertion,
        runner=runner,
    )

    assert value == Answer(answer="ok")
    assert observed["settings"] == {"sandbox": {"enabled": False}}
    for name in CREDENTIAL_ENVIRONMENT:
        assert name not in observed["environment"]


def test_argv_is_a_pure_function_of_profile_workspace_and_schema(tmp_path):
    argv = build_argv(
        tmp_path / "claude",
        profile=SessionProfile("test", turn_budget=3, timeout_seconds=20),
        workspace=tmp_path / "workspace",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        system_prompt="system",
        user_prompt="user",
        settings=tmp_path / "settings.json",
        mcp_config=tmp_path / "mcp.json",
    )

    assert argv == [
        str(tmp_path / "claude"),
        "--print",
        "--output-format",
        "json",
        "--json-schema",
        '{"properties":{"answer":{"type":"string"}},"type":"object"}',
        "--system-prompt",
        "system",
        "--max-turns",
        "3",
        "--no-session-persistence",
        "--settings",
        str(tmp_path / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(tmp_path / "mcp.json"),
        "--allowed-tools",
        "search_code",
        "--add-dir",
        str(tmp_path / "workspace"),
        "user",
    ]


def test_the_session_boundary_flags_are_never_conditional(tmp_path):
    """A no-tools profile is the case that used to drop all three.

    Without `--strict-mcp-config` Claude reads the `.mcp.json` of the repository
    under review; without `--settings` it runs with no `failIfUnavailable` and
    silently degrades to no sandbox; without `--allowed-tools` a profile that
    names no tools still gets Claude's defaults, which include Bash.
    """

    argv = build_argv(
        tmp_path / "claude",
        profile=SessionProfile(
            "toolless",
            turn_budget=1,
            timeout_seconds=5,
            tool_allowlist=(),
            needs_workspace=False,
        ),
        workspace=tmp_path / "workspace",
        schema={"type": "object"},
        system_prompt="system",
        user_prompt="user",
        settings=tmp_path / "settings.json",
        mcp_config=tmp_path / "mcp.json",
    )

    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--settings") + 1] == str(tmp_path / "settings.json")
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")
    assert argv[argv.index("--allowed-tools") + 1] == ""


def test_a_toolless_session_still_declares_an_empty_server_map(tmp_path):
    """"No tools" has to be stated, or the repository's own file supplies it."""

    path = write_mcp_config(tmp_path / "mcp.json", bridge_url=None)

    assert json.loads(path.read_text()) == {"mcpServers": {}}


def test_the_bridge_token_never_reaches_the_mcp_config_or_argv(tmp_path):
    """The URL carries a bearer token, so argv would publish it to `ps`."""

    class Provider:
        def search_code(self, query, *, path_prefix=None, limit=8):
            return {"matches": []}

    with McpBridge(Provider()) as bridge:
        path = write_mcp_config(tmp_path / "mcp.json", bridge_url=bridge.url)
        document = json.loads(path.read_text())
        server = document["mcpServers"]["diffuse-review-tools"]

        assert bridge.url not in json.dumps(document)
        assert server["args"] == ["-m", "diffuse_host.context_server"]


def test_session_uses_exact_agent_environment_and_validates_a_string_result(
    executable, monkeypatch, tmp_path
):
    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.setenv(name, "must-not-reach-agent")
    captured: dict[str, object] = {}

    def runner(argv, environment, cwd, timeout):
        captured.update(argv=argv, environment=environment, cwd=cwd, timeout=timeout)
        return investigation.SessionRun(0, _envelope('{"answer":"ok"}'), "")

    value, prompt_tokens, completion_tokens = investigation.run_structured(
        CLAUDE_CODE,
        Answer,
        system_prompt="system",
        user_prompt="user",
        workspace=tmp_path,
        tools=None,
        profile=REVIEW,
        runner=runner,
    )

    assert value == Answer(answer="ok")
    assert (prompt_tokens, completion_tokens) == (3, 5)
    environment = captured["environment"]
    assert captured["cwd"] == tmp_path
    assert captured["timeout"] == REVIEW.timeout_seconds
    assert environment["PATH"].endswith("/bin")
    assert environment["HOME"] != str(Path.home())
    for name in CREDENTIAL_ENVIRONMENT:
        assert name not in environment


def test_session_accepts_an_object_result(executable, tmp_path):
    def runner(*_args):
        return investigation.SessionRun(0, _envelope({"answer": "object"}), "")

    value, _, _ = investigation.run_structured(
        CLAUDE_CODE,
        Answer,
        system_prompt="s",
        user_prompt="u",
        workspace=tmp_path,
        tools=None,
        profile=REVIEW,
        runner=runner,
    )
    assert value.answer == "object"


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        ("not json", AgentInvestigationOutputError),
        (
            _envelope("", is_error=True, subtype="error_during_execution"),
            AgentInvestigationExecutionError,
        ),
        (
            _envelope("rate limit", is_error=True, subtype="error_during_execution"),
            AgentInvestigationRateLimited,
        ),
        (
            _envelope(
                "",
                mcp_servers=[{"name": "diffuse-review-tools", "status": "failed"}],
            ),
            AgentInvestigationMcpError,
        ),
    ],
)
def test_envelope_taxonomy(stdout, error):
    with pytest.raises(error):
        parse_envelope(stdout)


def test_a_healthy_mcp_server_is_not_a_failure_because_of_its_name():
    """The old check scanned the serialized blob for "fail" and "error"."""

    envelope = parse_envelope(
        _envelope(
            '{"answer":"ok"}',
            mcp_servers=[
                {"name": "error-budget-tools", "status": "connected"},
                {"name": "failover-inspector", "status": "connected"},
            ],
        )
    )

    assert envelope.result == '{"answer":"ok"}'


def test_an_author_in_the_message_is_not_a_credential_failure():
    """`AgentInvestigationTerminalError` is non-retryable, so `auth` in `author` kills a job."""

    with pytest.raises(AgentInvestigationExecutionError):
        parse_envelope(
            _envelope(
                "could not classify the commit author",
                is_error=True,
                subtype="error_during_execution",
            )
        )


def test_schema_mismatch_is_a_retryable_output_error(executable, tmp_path):
    def runner(*_args):
        return investigation.SessionRun(0, _envelope("not a matching object"), "")

    with pytest.raises(AgentInvestigationOutputError):
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )


def test_auth_exit_is_terminal_with_login_remedy(executable, tmp_path):
    def runner(*_args):
        return investigation.SessionRun(1, "", "authentication required")

    with pytest.raises(AgentInvestigationTerminalError, match="diffuse agent login claude"):
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )


def test_max_turns_retries_once_with_raised_budget(executable, tmp_path):
    """Note the non-zero exit: Claude exits 1 whenever it sets `is_error`.

    Classifying on the exit code first would collapse `error_max_turns` into a
    generic failure carrying the whole JSON document, and this retry would never
    happen. The envelope is read first for exactly that reason.
    """

    budgets: list[str] = []

    def runner(argv, *_args):
        budgets.append(argv[argv.index("--max-turns") + 1])
        if len(budgets) == 1:
            return investigation.SessionRun(
                1, _envelope("", is_error=True, subtype="error_max_turns"), ""
            )
        return investigation.SessionRun(0, _envelope({"answer": "covered"}), "")

    value, prompt_tokens, completion_tokens = investigation.run_structured(
        CLAUDE_CODE,
        Answer,
        system_prompt="s",
        user_prompt="u",
        workspace=tmp_path,
        tools=None,
        profile=REVIEW,
        runner=runner,
    )
    assert value.answer == "covered"
    assert budgets == ["24", "48"]
    # Both attempts, because the vendor billed for both: 3 + 3 and 5 + 5.
    assert (prompt_tokens, completion_tokens) == (6, 10)


def test_total_session_timeout_bounds_a_raised_turn_retry(executable, monkeypatch, tmp_path):
    """A native investigation's retry shares its capability/request time budget."""

    monotonic_values = iter((0.0, 0.0, 480.1))
    monkeypatch.setattr(investigation.time, "monotonic", lambda: next(monotonic_values))
    budgets: list[str] = []
    timeouts: list[int] = []

    def runner(argv, _environment, _cwd, timeout):
        budgets.append(argv[argv.index("--max-turns") + 1])
        timeouts.append(timeout)
        if len(budgets) == 1:
            return investigation.SessionRun(
                1, _envelope("", is_error=True, subtype="error_max_turns"), ""
            )
        return investigation.SessionRun(0, _envelope({"answer": "covered"}), "")

    value, _, _ = investigation.run_structured(
        CLAUDE_CODE,
        Answer,
        system_prompt="s",
        user_prompt="u",
        workspace=tmp_path,
        tools=None,
        profile=REVIEW,
        total_timeout_seconds=600,
        runner=runner,
    )

    assert value.answer == "covered"
    assert budgets == ["24", "48"]
    assert timeouts == [600, 120]


def test_a_rate_limit_survives_a_non_zero_exit(executable, tmp_path):
    """The envelope names the failure; the exit code only says there was one."""

    def runner(*_args):
        return investigation.SessionRun(
            1,
            _envelope("rate limit reached", is_error=True, subtype="error_during_execution"),
            "",
        )

    with pytest.raises(AgentInvestigationRateLimited, match="rate limit"):
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )


def test_second_max_turns_is_an_explicit_coverage_caveat(executable, tmp_path):
    def runner(*_args):
        return investigation.SessionRun(
            1,
            _envelope("", is_error=True, subtype="error_max_turns"),
            "",
        )

    with pytest.raises(AgentInvestigationCoverageCaveat, match="coverage caveat") as raised:
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )

    # An adapter turning this into a report caveat still has to bill both runs.
    assert raised.value.usage == (6, 10)


def test_a_crash_with_no_envelope_falls_back_to_the_exit_code(executable, tmp_path):
    """The exit code is the witness only when there is nothing better to read."""

    def runner(*_args):
        return investigation.SessionRun(137, "", "Killed")

    with pytest.raises(AgentInvestigationError, match="Killed"):
        investigation.run_structured(
            CLAUDE_CODE,
            Answer,
            system_prompt="s",
            user_prompt="u",
            workspace=tmp_path,
            tools=None,
            profile=REVIEW,
            runner=runner,
        )


def test_subprocess_runner_kills_the_entire_process_group_on_timeout(monkeypatch, tmp_path):
    class Process:
        pid = 123
        returncode = -15
        calls = 0

        def communicate(self, *, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("claude", timeout)
            return "", ""

    process = Process()
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(investigation.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(investigation.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(AgentInvestigationTimeout):
        investigation.subprocess_runner(["claude"], {}, tmp_path, 1)
    assert killed == [(123, investigation.signal.SIGTERM)]


def test_replay_cassette_rejects_a_different_invocation(tmp_path):
    path = tmp_path / "investigation.json"
    transcript = SessionTranscript(
        ("claude", "--print", "question"), 0, _envelope({"answer": "ok"}), ""
    )
    record(path, transcript)
    assert replay(path, list(transcript.argv)) == transcript
    with pytest.raises(Exception, match="does not match"):
        replay(path, ["claude", "--print", "other question"])


def test_per_session_mcp_bridge_only_exposes_search_code(tmp_path):
    calls: list[tuple[str, str | None, int]] = []

    class Provider:
        def search_code(self, query, *, path_prefix=None, limit=8):
            calls.append((query, path_prefix, limit))
            return {"matches": []}

    with McpBridge(Provider()) as bridge:
        write_mcp_config(tmp_path / "mcp.json", bridge_url=bridge.url)
        request = Request(
            f"{bridge.url}/search_code",
            data=json.dumps({"query": "needle", "path_prefix": "src", "limit": 2}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:  # noqa: S310 - ephemeral loopback URL
            assert json.loads(response.read()) == {"matches": []}
    assert calls == [("needle", "src", 2)]


def test_the_bridge_clamps_the_limit_an_agent_asks_for(tmp_path):
    """An untrusted diff steers the agent, so `limit` is the agent's, not ours."""

    calls: list[int] = []

    class Provider:
        def search_code(self, query, *, path_prefix=None, limit=8):
            calls.append(limit)
            return {"matches": []}

    with McpBridge(Provider()) as bridge:
        request = Request(
            f"{bridge.url}/search_code",
            data=json.dumps({"query": "needle", "limit": 10_000}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:  # noqa: S310 - ephemeral loopback URL
            response.read()

    assert calls == [MAX_SEARCH_LIMIT]


def test_a_stopped_bridge_does_not_hand_out_a_dead_address(tmp_path):
    class Provider:
        def search_code(self, query, *, path_prefix=None, limit=8):
            return {"matches": []}

    bridge = McpBridge(Provider())
    with bridge:
        assert bridge.url.startswith("http://127.0.0.1:")

    with pytest.raises(RuntimeError, match="has not started"):
        _ = bridge.url
