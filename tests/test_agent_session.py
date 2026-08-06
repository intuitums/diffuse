"""Contract coverage for the offline-testable agent session primitive."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from pydantic import BaseModel

from service.agents import session
from service.agents.claude_session import build_argv, parse_envelope
from service.agents.errors import (
    AgentSessionCoverageCaveat,
    AgentSessionExecutionError,
    AgentSessionMcpError,
    AgentSessionOutputError,
    AgentSessionRateLimited,
    AgentSessionTerminalError,
    AgentSessionTimeout,
)
from service.agents.mcp_bridge import McpBridge, write_mcp_config
from service.agents.profiles import REVIEW, SessionProfile
from service.agents.replay import SessionTranscript, record, replay
from service.review.agent_host import CLAUDE_CODE, CREDENTIAL_ENVIRONMENT


class Answer(BaseModel):
    answer: str


def _envelope(result: object, **extra: object) -> str:
    return json.dumps({"result": result, "usage": {"input_tokens": 3, "output_tokens": 5}, **extra})


@pytest.fixture
def executable(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "claude"
    path.write_text("")
    monkeypatch.setattr(session, "resolve_executable", lambda _cli: path)
    return path


def test_argv_is_a_pure_function_of_profile_workspace_and_schema(tmp_path):
    argv = build_argv(
        tmp_path / "claude",
        profile=SessionProfile("test", turn_budget=3, timeout_seconds=20),
        workspace=tmp_path / "workspace",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        system_prompt="system",
        user_prompt="user",
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
        "--add-dir",
        str(tmp_path / "workspace"),
        "--strict-mcp-config",
        "--mcp-config",
        str(tmp_path / "mcp.json"),
        "user",
    ]


def test_session_uses_exact_agent_environment_and_validates_a_string_result(
    executable, monkeypatch, tmp_path
):
    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.setenv(name, "must-not-reach-agent")
    captured: dict[str, object] = {}

    def runner(argv, environment, cwd, timeout):
        captured.update(argv=argv, environment=environment, cwd=cwd, timeout=timeout)
        return session.SessionRun(0, _envelope('{"answer":"ok"}'), "")

    value, prompt_tokens, completion_tokens = session.run_structured(
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
        return session.SessionRun(0, _envelope({"answer": "object"}), "")

    value, _, _ = session.run_structured(
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
        ("not json", AgentSessionOutputError),
        (
            _envelope("", is_error=True, subtype="error_during_execution"),
            AgentSessionExecutionError,
        ),
        (
            _envelope("rate limit", is_error=True, subtype="error_during_execution"),
            AgentSessionRateLimited,
        ),
        (_envelope("", mcp_servers={"diffuse-review-tools": "failed"}), AgentSessionMcpError),
    ],
)
def test_envelope_taxonomy(stdout, error):
    with pytest.raises(error):
        parse_envelope(stdout)


def test_schema_mismatch_is_a_retryable_output_error(executable, tmp_path):
    def runner(*_args):
        return session.SessionRun(0, _envelope("not a matching object"), "")

    with pytest.raises(AgentSessionOutputError):
        session.run_structured(
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
        return session.SessionRun(1, "", "authentication required")

    with pytest.raises(AgentSessionTerminalError, match="diffuse agent login claude"):
        session.run_structured(
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
    budgets: list[str] = []

    def runner(argv, *_args):
        budgets.append(argv[argv.index("--max-turns") + 1])
        if len(budgets) == 1:
            return session.SessionRun(
                0, _envelope("", is_error=True, subtype="error_max_turns"), ""
            )
        return session.SessionRun(0, _envelope({"answer": "covered"}), "")

    value, _, _ = session.run_structured(
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


def test_second_max_turns_is_an_explicit_coverage_caveat(executable, tmp_path):
    def runner(*_args):
        return session.SessionRun(0, _envelope("", is_error=True, subtype="error_max_turns"), "")

    with pytest.raises(AgentSessionCoverageCaveat, match="coverage caveat"):
        session.run_structured(
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
    monkeypatch.setattr(session.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(session.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(AgentSessionTimeout):
        session.subprocess_runner(["claude"], {}, tmp_path, 1)
    assert killed == [(123, session.signal.SIGTERM)]


def test_replay_cassette_rejects_a_different_invocation(tmp_path):
    path = tmp_path / "session.json"
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
        config_path = write_mcp_config(tmp_path / "mcp.json", bridge_url=bridge.url)
        config = json.loads(config_path.read_text())
        command = config["mcpServers"]["diffuse-review-tools"]
        assert command["args"][-1] == bridge.url
        request = Request(
            f"{bridge.url}/search_code",
            data=json.dumps({"query": "needle", "path_prefix": "src", "limit": 2}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:  # noqa: S310 - ephemeral loopback URL
            assert json.loads(response.read()) == {"matches": []}
    assert calls == [("needle", "src", 2)]
