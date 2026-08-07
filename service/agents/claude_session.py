"""Pure Claude Code argv construction and JSON-envelope parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from service.agents.errors import (
    AgentSessionAuthRequired,
    AgentSessionCoverageCaveat,
    AgentSessionExecutionError,
    AgentSessionMcpError,
    AgentSessionOutputError,
    AgentSessionRateLimited,
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
    settings: Path,
    mcp_config: Path,
) -> list[str]:
    """Build a single non-interactive Claude Code call without spawning it.

    Claude Code 2.1.220 documents `--json-schema` as an inline JSON argument
    and requires `--print` for both that option and JSON output.  Compact,
    stable serialization makes this vector suitable for replay fixtures.

    Three of these arguments are the session's boundary rather than its
    configuration, and all three are unconditional on purpose:

    - `--strict-mcp-config` with a Diffuse-written `--mcp-config`. Without it
      Claude also loads project-scoped `.mcp.json` -- and the project here is
      the repository under review, which is the untrusted input. A session with
      no tools still passes an empty server map, because "no tools" must mean
      no tools rather than whatever the repository declared.
    - `--allowed-tools` from the profile. The allowlist is the tool policy; a
      profile that names `search_code` must not also get Bash and Write just
      because they are Claude's defaults.
    - `--settings` naming the sandbox policy `agent_host` owns. That file
      carries `failIfUnavailable`, so without it a host missing bubblewrap
      warns once and runs the review unsandboxed.
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
        "--settings",
        str(settings),
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--allowed-tools",
        ",".join(profile.tool_allowlist),
    ]
    if profile.needs_workspace:
        argv.extend(("--add-dir", str(workspace)))
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


#: Statuses `mcp_servers[].status` reports for a server that is usable.
_HEALTHY_MCP_STATUSES = frozenset({"connected", "ready"})

#: Substrings that identify a quota failure in vendor prose. Unavoidably
#: heuristic -- the envelope has no machine-readable code for it -- but applied
#: only to the message, never to a whole serialized structure.
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "seat quota", "quota exceeded")

#: Likewise for credential failures. Whole words, because `auth` as a substring
#: also matches `author`, and a review CLI says `author` for ordinary reasons --
#: `AgentSessionTerminalError` is a `ValueError`, which the worker treats as
#: non-retryable, so a false positive here kills the job permanently.
CREDENTIAL_MARKERS = re.compile(
    r"\b(auth|authenticate|authentication|authorization|unauthorized"
    r"|login|log in|signed out|credential|credentials|api key)\b"
)


def _failed_mcp_servers(mcp_servers: object) -> list[str]:
    """Name the servers that did not come up, by status rather than by prose.

    Scanning the serialized blob for "fail" or "error" also matches a server
    *name*, a filesystem path, and any status string containing either word, so
    a healthy session could be failed by what its tools were called.
    """

    if not isinstance(mcp_servers, list):
        return []
    failed = []
    for entry in mcp_servers:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, str) and status.lower() not in _HEALTHY_MCP_STATUSES:
            failed.append(str(entry.get("name", "<unnamed>")))
    return failed


def _raise_envelope_failure(raw: dict[str, Any]) -> None:
    failed_servers = _failed_mcp_servers(raw.get("mcp_servers"))
    if failed_servers:
        raise AgentSessionMcpError(
            "Claude Code reported that an MCP server failed to start: "
            + ", ".join(failed_servers)
        )
    if raw.get("is_error") is not True:
        return
    subtype = str(raw.get("subtype", ""))
    detail = str(raw.get("result", subtype or "Claude Code reported an error"))
    lowered = f"{subtype} {detail}".lower()
    if subtype == "error_max_turns":
        usage = raw.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        raise AgentSessionCoverageCaveat(
            "Claude Code exhausted its turn budget",
            prompt_tokens=_nonnegative_usage(usage, "input_tokens", "prompt_tokens"),
            completion_tokens=_nonnegative_usage(usage, "output_tokens", "completion_tokens"),
        )
    if any(marker in lowered for marker in RATE_LIMIT_MARKERS):
        raise AgentSessionRateLimited(detail)
    if CREDENTIAL_MARKERS.search(lowered):
        raise AgentSessionAuthRequired("Claude authentication is required")
    raise AgentSessionExecutionError(detail)
