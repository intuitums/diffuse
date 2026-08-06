"""Pure Claude Code argv construction and JSON-envelope parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from service.agents.errors import (
    AgentSessionCoverageCaveat,
    AgentSessionExecutionError,
    AgentSessionMcpError,
    AgentSessionOutputError,
    AgentSessionRateLimited,
    AgentSessionTerminalError,
)
from service.agents.profiles import SessionProfile


@dataclass(frozen=True)
class ClaudeEnvelope:
    """The response payload and accounted tokens from one Claude turn."""

    result: object
    prompt_tokens: int
    completion_tokens: int


def build_argv(
    executable: Path,
    *,
    profile: SessionProfile,
    workspace: Path,
    schema: Mapping[str, object],
    system_prompt: str,
    user_prompt: str,
    mcp_config: Path | None = None,
) -> list[str]:
    """Build a single non-interactive Claude Code call without spawning it.

    Claude Code 2.1.220 documents `--json-schema` as an inline JSON argument
    and requires `--print` for both that option and JSON output.  Compact,
    stable serialization makes this vector suitable for replay fixtures.
    """

    argv = [
        str(executable),
        "--print",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":"), sort_keys=True),
        "--system-prompt",
        system_prompt,
        "--max-turns",
        str(profile.turn_budget),
        "--no-session-persistence",
    ]
    if profile.needs_workspace:
        argv.extend(("--add-dir", str(workspace)))
    if mcp_config is not None and profile.tool_allowlist:
        argv.extend(("--strict-mcp-config", "--mcp-config", str(mcp_config)))
    argv.append(user_prompt)
    return argv


def parse_envelope(stdout: str) -> ClaudeEnvelope:
    """Parse Claude's single-result JSON envelope into Diffuse's stable shape."""

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AgentSessionOutputError("Claude Code did not emit a JSON envelope") from error
    if not isinstance(raw, dict):
        raise AgentSessionOutputError("Claude Code emitted a non-object JSON envelope")

    _raise_envelope_failure(raw)
    if "result" not in raw:
        raise AgentSessionOutputError("Claude Code envelope did not include result")
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return ClaudeEnvelope(
        result=raw["result"],
        prompt_tokens=_nonnegative_usage(usage, "input_tokens", "prompt_tokens"),
        completion_tokens=_nonnegative_usage(usage, "output_tokens", "completion_tokens"),
    )


def _nonnegative_usage(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _raise_envelope_failure(raw: dict[str, Any]) -> None:
    mcp_servers = raw.get("mcp_servers")
    if mcp_servers is not None and any(
        word in json.dumps(mcp_servers).lower() for word in ("fail", "error")
    ):
        raise AgentSessionMcpError("Claude Code reported that an MCP server failed to start")
    if raw.get("is_error") is not True:
        return
    subtype = str(raw.get("subtype", ""))
    detail = str(raw.get("result", subtype or "Claude Code reported an error"))
    lowered = f"{subtype} {detail}".lower()
    if subtype == "error_max_turns":
        raise AgentSessionCoverageCaveat("Claude Code exhausted its turn budget")
    if "rate limit" in lowered or "rate_limit" in lowered or "seat quota" in lowered:
        raise AgentSessionRateLimited(detail)
    if subtype == "error_during_execution":
        raise AgentSessionExecutionError(detail)
    if "auth" in lowered or "login" in lowered or "credential" in lowered:
        raise AgentSessionTerminalError(f"{detail}; run `diffuse agent login claude`")
    raise AgentSessionExecutionError(detail)
