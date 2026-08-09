"""Explicit agent-runtime configuration shared by control plane and runner.

A deployment must choose `claude` or `codex` for the CLI-native path. There is
no default runtime in the target architecture. This module freezes the names
and budget contract used by Agent Dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass

AGENT_RUNTIME_CLAUDE = "claude"
AGENT_RUNTIME_CODEX = "codex"

#: Runtimes the isolated agent-host may execute. Not the same as
#: `service.review.agents.RUNTIME_NAMES`, which selects review Agents.
AGENT_RUNTIME_NAMES: tuple[str, ...] = (AGENT_RUNTIME_CLAUDE, AGENT_RUNTIME_CODEX)

DEFAULT_TURN_BUDGET = 24
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RESULT_BYTES = 256_000


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Budgets and identity for one CLI-native agent session.

    The control plane and runner must agree on these fields before a session
    starts. Values are validated at construction so a misconfigured deployment
    fails before minting a capability.
    """

    runtime: str
    turn_budget: int = DEFAULT_TURN_BUDGET
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        if self.runtime not in AGENT_RUNTIME_NAMES:
            raise ValueError(
                f"runtime must be one of {', '.join(AGENT_RUNTIME_NAMES)}; "
                f"got {self.runtime!r}"
            )
        if self.turn_budget <= 0:
            raise ValueError("turn_budget must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_result_bytes <= 0:
            raise ValueError("max_result_bytes must be positive")


def parse_agent_runtime_name(value: str) -> str:
    """Return a canonical agent-runtime name or raise ``ValueError``."""

    normalized = value.strip().lower()
    if normalized not in AGENT_RUNTIME_NAMES:
        raise ValueError(
            f"{value!r} is not an agent runtime; use one of: "
            f"{', '.join(AGENT_RUNTIME_NAMES)}"
        )
    return normalized
