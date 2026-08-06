"""A testable structured agent-CLI session runner."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ValidationError

from service.agents.claude_session import build_argv, parse_envelope
from service.agents.errors import (
    AgentSessionCoverageCaveat,
    AgentSessionError,
    AgentSessionOutputError,
    AgentSessionRateLimited,
    AgentSessionTerminalError,
    AgentSessionTimeout,
)
from service.agents.mcp_bridge import McpBridge, write_mcp_config
from service.agents.profiles import SessionProfile
from service.agents.replay import SessionTranscript
from service.review.agent_host import (
    AgentCli,
    agent_environment,
    agent_scratch_directory,
    resolve_executable,
)
from service.review.tools import ReviewToolProvider


@dataclass(frozen=True)
class SessionRun:
    """The subprocess result passed through the injectable runner seam."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], dict[str, str], Path, int], SessionRun]


def subprocess_runner(argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> SessionRun:
    """Run an agent in a new process group and remove that group on timeout."""

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        # Agent CLIs spawn MCP and sandbox descendants. Killing only the parent
        # leaks those processes and lets them keep touching a completed review.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AgentSessionTimeout(f"Agent session exceeded {timeout} seconds") from error
    return SessionRun(process.returncode, stdout, stderr)


def run_structured[T: BaseModel](
    cli: AgentCli,
    response_model: type[T],
    *,
    system_prompt: str,
    user_prompt: str,
    workspace: Path,
    tools: ReviewToolProvider | None,
    profile: SessionProfile,
    runner: Runner = subprocess_runner,
) -> tuple[T, int, int]:
    """Run one structured CLI turn and mirror `_call_structured`'s return shape.

    No caller is wired to this primitive yet.  `runner` is deliberately an
    argument rather than a module-global test hook: a cassette or fake can
    exercise every branch without a CLI, credential, network, or subprocess.
    """

    executable = resolve_executable(cli)
    schema = response_model.model_json_schema()
    bridge_context = (
        McpBridge(tools) if tools is not None and profile.tool_allowlist else _NullBridge()
    )
    with (
        agent_scratch_directory() as scratch,
        TemporaryDirectory(prefix="diffuse-agent-mcp-") as config_directory,
        bridge_context as bridge,
    ):
        mcp_config = (
            write_mcp_config(Path(config_directory) / "mcp.json", bridge_url=bridge.url)
            if bridge is not None
            else None
        )
        return _run_once_or_raised_budget(
            executable,
            cli=cli,
            response_model=response_model,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workspace=workspace,
            mcp_config=mcp_config,
            profile=profile,
            environment=agent_environment(cli, scratch=scratch),
            runner=runner,
        )


class _NullBridge:
    """Keep the optional MCP bridge out of the session's no-tools path."""

    url: str | None = None

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


def _run_once_or_raised_budget[T: BaseModel](
    executable: Path,
    *,
    cli: AgentCli,
    response_model: type[T],
    schema: dict[str, object],
    system_prompt: str,
    user_prompt: str,
    workspace: Path,
    mcp_config: Path | None,
    profile: SessionProfile,
    environment: dict[str, str],
    runner: Runner,
) -> tuple[T, int, int]:
    try:
        return _run_once(
            executable,
            cli=cli,
            response_model=response_model,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workspace=workspace,
            mcp_config=mcp_config,
            profile=profile,
            environment=environment,
            runner=runner,
        )
    except AgentSessionCoverageCaveat:
        try:
            return _run_once(
                executable,
                cli=cli,
                response_model=response_model,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                workspace=workspace,
                mcp_config=mcp_config,
                profile=profile.with_raised_budget(),
                environment=environment,
                runner=runner,
            )
        except AgentSessionCoverageCaveat as error:
            raise AgentSessionCoverageCaveat(
                "Agent exhausted the raised turn budget; attach a coverage caveat to the report"
            ) from error


def _run_once[T: BaseModel](
    executable: Path,
    *,
    cli: AgentCli,
    response_model: type[T],
    schema: dict[str, object],
    system_prompt: str,
    user_prompt: str,
    workspace: Path,
    mcp_config: Path | None,
    profile: SessionProfile,
    environment: dict[str, str],
    runner: Runner,
) -> tuple[T, int, int]:
    if cli.runtime != "claude":
        raise AgentSessionTerminalError(f"No session adapter exists for {cli.runtime!r}")
    argv = build_argv(
        executable,
        profile=profile,
        workspace=workspace,
        schema=schema,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        mcp_config=mcp_config,
    )
    run = runner(argv, environment, workspace, profile.timeout_seconds)
    _raise_exit_failure(run)
    envelope = parse_envelope(run.stdout)
    try:
        raw_result = envelope.result
        document = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
        value = response_model.model_validate_json(document)
    except (TypeError, ValueError, ValidationError) as error:
        raise AgentSessionOutputError(
            "Claude Code result did not match the response schema"
        ) from error
    return value, envelope.prompt_tokens, envelope.completion_tokens


def _raise_exit_failure(run: SessionRun) -> None:
    if run.returncode == 0:
        return
    detail = run.stderr.strip() or run.stdout.strip() or f"agent exit code {run.returncode}"
    lowered = detail.lower()
    if "auth" in lowered or "login" in lowered or "credential" in lowered:
        raise AgentSessionTerminalError(f"{detail}; run `diffuse agent login claude`")
    if "rate limit" in lowered or "rate_limit" in lowered or "seat quota" in lowered:
        raise AgentSessionRateLimited(detail)
    raise AgentSessionError(detail)


def transcript_runner(transcript: SessionTranscript) -> Runner:
    """Adapt a cassette transcript to the runner seam after matching argv."""

    def run(argv: list[str], _env: dict[str, str], _cwd: Path, _timeout: int) -> SessionRun:
        if tuple(argv) != transcript.argv:
            raise AgentSessionError("Agent session cassette does not match this invocation")
        return SessionRun(transcript.returncode, transcript.stdout, transcript.stderr)

    return run
