"""Codex ``exec`` argv construction and JSONL result parsing.

Codex emits lifecycle events rather than Claude's single envelope.  Keeping
that vendor detail here gives the runner one structured result contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diffuse_protocol.profiles import SessionProfile

from diffuse_host.claude import CREDENTIAL_MARKERS, RATE_LIMIT_MARKERS
from diffuse_host.errors import (
    AgentInvestigationAuthRequired,
    AgentInvestigationExecutionError,
    AgentInvestigationMcpError,
    AgentInvestigationOutputError,
    AgentInvestigationRateLimited,
)


@dataclass(frozen=True)
class CodexEnvelope:
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
    mcp_config: Path,
) -> list[str]:
    """Build a noninteractive, read-only Codex execution.

    The prompt is deliberately supplied as one argument: user-controlled
    repository data cannot become a shell fragment, and Codex's JSONL protocol
    remains the sole result channel.
    """

    prompt = f"{system_prompt}\n\n{user_prompt}"
    return [
        str(executable),
        "exec",
        "--json",
        "--output-schema",
        json.dumps(schema, separators=(",", ":"), sort_keys=True),
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--mcp-config",
        str(mcp_config),
        "--skip-git-repo-check",
        prompt,
    ]


def parse_envelope(stdout: str) -> CodexEnvelope:
    """Extract the completed structured result from Codex JSONL events."""

    result: object | None = None
    prompt_tokens = completion_tokens = 0
    saw_event = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AgentInvestigationOutputError("Codex did not emit JSONL events") from error
        if not isinstance(event, dict):
            raise AgentInvestigationOutputError("Codex emitted a non-object JSONL event")
        saw_event = True
        _raise_event_failure(event)
        usage = event.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = max(prompt_tokens, _usage(usage, "input_tokens", "prompt_tokens"))
            completion_tokens = max(
                completion_tokens, _usage(usage, "output_tokens", "completion_tokens")
            )
        if event.get("type") in {"item.completed", "turn.completed", "response.completed"}:
            candidate = event.get("result", event.get("output"))
            if candidate is not None:
                result = candidate
    if not saw_event or result is None:
        raise AgentInvestigationOutputError("Codex did not emit a completed structured result")
    return CodexEnvelope(result, prompt_tokens, completion_tokens)


def _usage(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _raise_event_failure(event: dict[str, Any]) -> None:
    if event.get("type") not in {"error", "turn.failed", "response.failed"}:
        return
    detail = str(event.get("message", event.get("error", "Codex reported an error")))
    lowered = detail.lower()
    if "mcp" in lowered:
        raise AgentInvestigationMcpError("Codex reported an MCP failure")
    if any(marker in lowered for marker in RATE_LIMIT_MARKERS):
        raise AgentInvestigationRateLimited(detail)
    if CREDENTIAL_MARKERS.search(lowered):
        raise AgentInvestigationAuthRequired("Codex authentication is required")
    raise AgentInvestigationExecutionError(detail)
