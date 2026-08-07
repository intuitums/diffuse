"""Selectable review runtimes.

A review runtime turns a `ReviewRequest` into a `ReviewReport`. Diffuse owns
the contract; the runtime supplies the investigation. See
`docs/agent-runtimes.md` and the CLI-native agent operation plan.

`litellm` remains the transitional one-shot implementation. CLI-native
`claude` and `codex` dispatch over the private runner-control network to an
isolated container; the worker never executes either vendor binary.

The seam is deliberately the whole report rather than a single model call.
`_call_structured` is the wrong altitude for it -- an agentic runtime that
drives a CLI supplies its own investigation loop, so a per-call seam would pay
that runtime's startup on every pass while keeping the single-shot structure
above it. Handing over the entire report is what lets a runtime decide how many
calls a review is, and it is the level `service/eval_harness.py` already scores,
so two runtimes are directly comparable on the committed fixtures without new
harness work.

Named for `service/review/`, not to be confused with `service/runtime.py`,
which is the frozen binary's entrypoint.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from service.models.review import ReviewReport
    from service.review.request import ReviewRequest

LITELLM_RUNTIME = "litellm"
#: CLI-native agent-runtime name. Matches `diffuse agent login claude` — the
#: short product name, not the `claude-code` binary nickname. Execution belongs
#: on the isolated agent-runner (Gate B/C), not the worker.
CLAUDE_CODE_RUNTIME = "claude"
CODEX_RUNTIME = "codex"

#: Every name `REVIEW_RUNTIME` accepts. A name is listed here once the adapter
#: exists, not when it is planned: an accepted name with no implementation is a
#: configuration that validates at startup and fails mid-review.
RUNTIME_NAMES: tuple[str, ...] = (LITELLM_RUNTIME, CLAUDE_CODE_RUNTIME, CODEX_RUNTIME)

#: Values the worker may dispatch. Native names are network clients only; the
#: worker never mounts credentials or executes their CLI.
HOSTED_RUNTIME_NAMES: tuple[str, ...] = (LITELLM_RUNTIME, CLAUDE_CODE_RUNTIME, CODEX_RUNTIME)

RUNTIME_VARIABLE = "REVIEW_RUNTIME"


class ReviewRuntime(Protocol):
    """Produces a complete `ReviewReport` from a `ReviewRequest`."""

    @property
    def name(self) -> str:
        """The `REVIEW_RUNTIME` value that selects this runtime."""

    def generate(self, request: ReviewRequest) -> ReviewReport:
        """Review `request.diff_text` (and whatever else the runtime needs).

        `candidate_model` and `verifier_model` stay on the request rather than
        being folded into the runtime because provenance routing chooses them
        per review (`service/review/provenance.py`): an agent-authored diff is
        deliberately reviewed by an opposing model family, and a runtime that
        could not express a candidate/verifier pair would lose that. A runtime
        is free to map the pair onto whatever it drives.
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


def review_runtime_name() -> str:
    """Resolve `REVIEW_RUNTIME`, defaulting to the one-shot LiteLLM runtime."""

    configured = os.environ.get(RUNTIME_VARIABLE, "").strip()
    if not configured:
        return LITELLM_RUNTIME
    if configured not in RUNTIME_NAMES:
        raise _unknown_runtime(configured)
    return configured


def hosted_review_runtime_name() -> str:
    """Resolve the runtime a worker may dispatch without executing a CLI."""

    name = review_runtime_name()
    if name not in HOSTED_RUNTIME_NAMES:
        raise ValueError(
            f"{RUNTIME_VARIABLE}={name} cannot be executed by the worker "
            f"process; agent-CLI runtimes run on the isolated agent-runner. "
            f"Transitional in-process runtimes: "
            f"{' or '.join(HOSTED_RUNTIME_NAMES)}"
        )
    return name


def resolve_review_runtime(name: str | None = None) -> ReviewRuntime:
    """Build the runtime `name` selects, defaulting to `REVIEW_RUNTIME`."""

    selected = name if name is not None else review_runtime_name()
    if selected == LITELLM_RUNTIME:
        # Imported here rather than at module scope: `engine` imports this
        # module for `review_runtime_name`, so a top-level import would be a
        # cycle.
        from service.review.engine import LiteLLMRuntime

        return LiteLLMRuntime()
    if selected in {CLAUDE_CODE_RUNTIME, CODEX_RUNTIME}:
        from service.review.native_runner import NativeRunnerRuntime

        return NativeRunnerRuntime(selected)
    raise _unknown_runtime(selected)
