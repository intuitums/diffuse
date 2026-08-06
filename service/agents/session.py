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

from service.agents.claude_session import (
    CREDENTIAL_MARKERS,
    RATE_LIMIT_MARKERS,
    ClaudeEnvelope,
    build_argv,
    parse_envelope,
)
from service.agents.errors import (
    AgentSessionCoverageCaveat,
    AgentSessionError,
    AgentSessionOutputError,
    AgentSessionRateLimited,
    AgentSessionTerminalError,
    AgentSessionTimeout,
)
from service.agents.mcp_bridge import BRIDGE_URL_VARIABLE, McpBridge, write_mcp_config
from service.agents.profiles import SessionProfile
from service.agents.replay import SessionTranscript
from service.review.agent_host import (
    AgentCli,
    agent_environment,
    agent_scratch_directory,
    resolve_executable,
    sandbox_settings,
    sandbox_settings_are_current,
)
from service.review.tools import ReviewToolProvider


def write_session_settings(path: Path, *, cli: AgentCli, worktree: Path) -> Path:
    """Render the owned sandbox policy for one review's worktree.

    The persisted policy under `CLAUDE_CONFIG_DIR` is worktree-independent, and
    the worktree is the one part that cannot live there because it differs per
    review. `agent_host.write_sandbox_settings` says as much: the adapter is
    expected to supply `allowRead` through `--settings` at call time. This is
    that supply. Everything else -- `failIfUnavailable`, `strictAllowlist`, the
    credential denies -- comes from `sandbox_settings` unchanged, so there is
    one definition of the policy and this only narrows what may be read.
    """

    document = sandbox_settings(worktree)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


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
    # The persisted policy carries `failIfUnavailable`, so a session that runs
    # without it silently degrades to no sandbox on a host missing bubblewrap.
    # Refuse a stale one too: it was written at login and is read on every
    # review after, so drift leaves a weaker boundary on disk than Diffuse
    # intends with nothing saying so.
    if not sandbox_settings_are_current(cli):
        raise AgentSessionTerminalError(
            f"{cli.display_name}'s Diffuse-owned sandbox policy is missing or out of "
            f"date; run `diffuse agent write-policy {cli.runtime}` before a session"
        )
    bridge_context = (
        McpBridge(tools) if tools is not None and profile.tool_allowlist else _NullBridge()
    )
    with (
        agent_scratch_directory() as scratch,
        TemporaryDirectory(prefix="diffuse-agent-mcp-") as config_directory,
        bridge_context as bridge,
    ):
        settings = write_session_settings(
            Path(config_directory) / "settings.json",
            cli=cli,
            worktree=workspace,
        )
        # Always written, even with no bridge. `--mcp-config` plus
        # `--strict-mcp-config` is what stops Claude reading the `.mcp.json` of
        # the repository under review, so a no-tools session needs an empty
        # declaration rather than no declaration.
        mcp_config = write_mcp_config(
            Path(config_directory) / "mcp.json",
            bridge_url=bridge.url if bridge is not None else None,
        )
        environment = agent_environment(cli, scratch=scratch)
        if bridge is not None:
            # The token authenticates the tool bridge, so it must not sit in the
            # child's argv where `ps` shows it to every local user.
            environment[BRIDGE_URL_VARIABLE] = bridge.url
        return _run_once_or_raised_budget(
            executable,
            cli=cli,
            response_model=response_model,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workspace=workspace,
            settings=settings,
            mcp_config=mcp_config,
            profile=profile,
            environment=environment,
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
    settings: Path,
    mcp_config: Path,
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
            settings=settings,
            mcp_config=mcp_config,
            profile=profile,
            environment=environment,
            runner=runner,
        )
    except AgentSessionCoverageCaveat as exhausted:
        # The abandoned attempt still spent a full turn budget. Carrying its
        # usage forward keeps the caller's cost accounting equal to what the
        # vendor actually bills; reporting only the retry understates it by
        # however far the first attempt got.
        spent_prompt, spent_completion = exhausted.usage
        try:
            value, prompt_tokens, completion_tokens = _run_once(
                executable,
                cli=cli,
                response_model=response_model,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                workspace=workspace,
                settings=settings,
                mcp_config=mcp_config,
                profile=profile.with_raised_budget(),
                environment=environment,
                runner=runner,
            )
        except AgentSessionCoverageCaveat as error:
            raise AgentSessionCoverageCaveat(
                "Agent exhausted the raised turn budget; attach a coverage caveat to the report",
                prompt_tokens=spent_prompt + error.prompt_tokens,
                completion_tokens=spent_completion + error.completion_tokens,
            ) from error
        return (
            value,
            prompt_tokens + spent_prompt,
            completion_tokens + spent_completion,
        )


def _run_once[T: BaseModel](
    executable: Path,
    *,
    cli: AgentCli,
    response_model: type[T],
    schema: dict[str, object],
    system_prompt: str,
    user_prompt: str,
    workspace: Path,
    settings: Path,
    mcp_config: Path,
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
        settings=settings,
        mcp_config=mcp_config,
    )
    run = runner(argv, environment, workspace, profile.timeout_seconds)
    # The envelope is classified before the exit code, not after. Claude exits
    # non-zero whenever it sets `is_error`, so checking the code first would
    # make every case the envelope distinguishes -- max turns, rate limit, a
    # dead MCP server -- collapse into one opaque error carrying the whole JSON
    # document as its message, and the raised-budget retry would never fire.
    # The exit code is the fallback for a CLI that died before emitting one.
    envelope = _parse_envelope_or_exit_failure(run)
    try:
        raw_result = envelope.result
        document = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
        value = response_model.model_validate_json(document)
    except (TypeError, ValueError, ValidationError) as error:
        raise AgentSessionOutputError(
            "Claude Code result did not match the response schema"
        ) from error
    return value, envelope.prompt_tokens, envelope.completion_tokens


def _parse_envelope_or_exit_failure(run: SessionRun) -> ClaudeEnvelope:
    """Prefer the envelope's own account of the failure over the exit code.

    An envelope that parses is always the better witness: it names the subtype,
    which is the only place `error_max_turns` is distinguishable from a real
    error. The exit code is consulted only when there is no envelope to read --
    a CLI that crashed, was killed, or wrote nothing.
    """

    try:
        return parse_envelope(run.stdout)
    except AgentSessionOutputError:
        if run.returncode == 0:
            raise
    _raise_exit_failure(run)
    raise AssertionError("unreachable: _raise_exit_failure never returns")


def _raise_exit_failure(run: SessionRun) -> None:
    detail = run.stderr.strip() or run.stdout.strip() or f"agent exit code {run.returncode}"
    lowered = detail.lower()
    if CREDENTIAL_MARKERS.search(lowered):
        raise AgentSessionTerminalError(f"{detail}; run `diffuse agent login claude`")
    if any(marker in lowered for marker in RATE_LIMIT_MARKERS):
        raise AgentSessionRateLimited(detail)
    raise AgentSessionError(detail)


def transcript_runner(transcript: SessionTranscript) -> Runner:
    """Adapt a cassette transcript to the runner seam after matching argv."""

    def run(argv: list[str], _env: dict[str, str], _cwd: Path, _timeout: int) -> SessionRun:
        if tuple(argv) != transcript.argv:
            raise AgentSessionError("Agent session cassette does not match this invocation")
        return SessionRun(transcript.returncode, transcript.stdout, transcript.stderr)

    return run
