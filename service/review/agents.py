"""Selectable review agents.

A review runtime turns a `ReviewRequest` into a `ReviewReport`. Diffuse owns
the contract; the runtime supplies the investigation. See
`docs/agents.md` and the CLI-native agent operation plan.

`claude` and `codex` dispatch over the private Agent Host network to an
isolated environment; the worker never executes either vendor binary.

The seam is deliberately the whole report rather than a single model call.
`_call_structured` is the wrong altitude for it -- an agentic runtime that
drives a CLI supplies its own investigation loop, so a per-call seam would pay
that runtime's startup on every pass while keeping the single-shot structure
above it. Handing over the entire report is what lets a runtime decide how many
calls a review is, so every Agent must return the same durable report contract.

Named for `service/review/`, not to be confused with `service/runtime.py`,
which is the frozen binary's entrypoint.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from service.models.review import ReviewReport
    from service.review.request import ReviewRequest

#: CLI-native Review Agent name. Matches `diffuse agent login claude` — the
#: short product name, not the `claude-code` binary nickname. Execution belongs
#: on the isolated Agent Host, never the worker.
CLAUDE_CODE_RUNTIME = "claude"
CODEX_RUNTIME = "codex"

#: Every name `REVIEW_AGENT` accepts. A name is listed here once the adapter
#: exists, not when it is planned: an accepted name with no implementation is a
#: configuration that validates at startup and fails mid-review.
RUNTIME_NAMES: tuple[str, ...] = (CLAUDE_CODE_RUNTIME, CODEX_RUNTIME)

#: Values the worker may dispatch. Native names are network clients only; the
#: worker never mounts credentials or executes their CLI.
HOSTED_RUNTIME_NAMES: tuple[str, ...] = RUNTIME_NAMES

RUNTIME_VARIABLE = "REVIEW_AGENT"


class ReviewAgent(Protocol):
    """Produces a complete `ReviewReport` from a `ReviewRequest`."""

    @property
    def name(self) -> str:
        """The `REVIEW_AGENT` value that selects this runtime."""

    def generate(self, request: ReviewRequest) -> ReviewReport:
        """Run the exact-head candidate and independent verifier investigations.

        The selected Review Agent determines the candidate Agent Host; the
        opposite supported engine verifies its structured result. Historical
        model-name fields remain on the request only for durable provenance.
        """


def _unknown_runtime(name: str) -> ValueError:
    """The one wording for a name Diffuse does not implement.

    Both entry points below reject the same set, so the message lives here
    rather than being written twice and drifting the day a runtime is added.
    """

    return ValueError(
        f"{RUNTIME_VARIABLE}={name!r} is not a runtime Diffuse implements; "
        f"use one of: {', '.join(RUNTIME_NAMES)}"
    )


def review_agent_name() -> str:
    """Resolve the explicitly configured review agent."""

    configured = os.environ.get(RUNTIME_VARIABLE, "").strip()
    if not configured:
        raise ValueError(
            f"{RUNTIME_VARIABLE} is required; use one of: {', '.join(RUNTIME_NAMES)}"
        )
    if configured not in RUNTIME_NAMES:
        raise _unknown_runtime(configured)
    return configured


def hosted_review_agent_name() -> str:
    """Resolve the runtime a worker may dispatch without executing a CLI."""

    name = review_agent_name()
    if name not in HOSTED_RUNTIME_NAMES:
        raise ValueError(
            f"{RUNTIME_VARIABLE}={name} cannot be executed by the worker "
            f"process; it must run on the isolated Agent Host."
        )
    return name


def resolve_review_agent(name: str | None = None) -> ReviewAgent:
    """Build the runtime `name` selects, defaulting to `REVIEW_AGENT`."""

    selected = name if name is not None else review_agent_name()
    if selected in {CLAUDE_CODE_RUNTIME, CODEX_RUNTIME}:
        from service.review.agent_client import AgentRuntime

        return AgentRuntime(selected)
    raise _unknown_runtime(selected)
