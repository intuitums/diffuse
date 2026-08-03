"""Selectable review runtimes.

A review runtime turns a diff plus context into a `ReviewReport`. Diffuse owns
the contract; the runtime supplies the investigation. See
`docs/agent-runtimes.md`.

`litellm` is the only selectable implementation today: the one-shot API path
that orchestrates candidate passes, deduplication, diagram, and verifier via
structured completions (implemented with the LiteLLM library). Planned
local-only values `claude-code` and `codex` are named here so host plumbing and
tests can refer to them, but they are not in `RUNTIME_NAMES` until an adapter
exists.

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
    from collections.abc import Callable

    from repository_policy.resolve import ResolvedReviewPolicy
    from retriever.retrieve import RetrievedContext
    from service.models.review import ReviewReport

LITELLM_RUNTIME = "litellm"
CLAUDE_CODE_RUNTIME = "claude-code"
CODEX_RUNTIME = "codex"

#: Every name `REVIEW_RUNTIME` accepts. A name is listed here once the adapter
#: exists, not when it is planned: an accepted name with no implementation is a
#: configuration that validates at startup and fails mid-review.
RUNTIME_NAMES: tuple[str, ...] = (LITELLM_RUNTIME,)

#: The runtimes the hosted worker may use. The agent-CLI runtimes drive a
#: developer's locally installed, locally authenticated CLI, so they are a
#: `diffuse review` capability and not a server one -- the hosted path reviews
#: pull requests from anyone who can open one, which is a different threat
#: model, and it has no CLI to drive in the first place.
HOSTED_RUNTIME_NAMES: tuple[str, ...] = (LITELLM_RUNTIME,)

RUNTIME_VARIABLE = "REVIEW_RUNTIME"


class ReviewRuntime(Protocol):
    """Produces a complete `ReviewReport` from a diff and its context."""

    @property
    def name(self) -> str:
        """The `REVIEW_RUNTIME` value that selects this runtime."""

    def generate(
        self,
        diff_text: str,
        contexts: list[RetrievedContext],
        *,
        progress_callback: Callable[[], None] | None = None,
        policy: ResolvedReviewPolicy | None = None,
        candidate_model: str | None = None,
        verifier_model: str | None = None,
    ) -> ReviewReport:
        """Review `diff_text`.

        `candidate_model` and `verifier_model` stay in the interface rather
        than being folded into the runtime because provenance routing chooses
        them per review (`service/review/provenance.py`): an agent-authored
        diff is deliberately reviewed by an opposing model family, and a
        runtime that could not express a candidate/verifier pair would lose
        that. A runtime is free to map the pair onto whatever it drives.
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
    """Resolve `REVIEW_RUNTIME` for a server process, refusing local runtimes.

    Separate from `review_runtime_name` so the refusal happens at worker
    startup -- named, once -- rather than per job. A runtime this process
    cannot drive would otherwise dead-letter every pull request in the fleet,
    and fixing the variable afterwards recovers none of them.
    """

    name = review_runtime_name()
    if name not in HOSTED_RUNTIME_NAMES:
        raise ValueError(
            f"{RUNTIME_VARIABLE}={name} is a local `diffuse review` runtime and "
            f"cannot be used by a server process; set it to "
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
    raise _unknown_runtime(selected)
