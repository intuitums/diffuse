"""Agent-only review execution compatibility boundary.

Diffuse deliberately has no direct model client. A configured Review Agent
receives one immutable investigation request and returns a structured report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from diffuse_protocol.review import ReviewReport

from diffuse.repository.policy.models import RepositoryPolicySnapshot
from diffuse.review.agents import ReviewAgent, resolve_review_agent
from diffuse.review.request import ReviewRequest

PROMPT_VERSION = "agent-v1"


@dataclass(frozen=True)
class ReviewDepthSupport:
    """Compatibility record retained for durable review metadata."""

    depth: str | None
    variable: str
    plans: tuple[object, ...]
    source: str


def generate_review(
    diff_text: str,
    contexts: list[Any],
    *,
    policy: RepositoryPolicySnapshot | None = None,
    progress_callback: Callable[[], None] | None = None,
    request: ReviewRequest | None = None,
    runtime: ReviewAgent | None = None,
    **_unused: Any,
) -> ReviewReport:
    """Run the configured isolated Review Agent.

    Model/provider selection is intentionally absent. Callers must supply the
    immutable workspace and dispatch created by the self-hosted worker.
    """

    selected_request = request or ReviewRequest(
        diff_text=diff_text,
        contexts=contexts,
        progress_callback=progress_callback,
        policy=policy,
    )
    return (runtime or resolve_review_agent()).generate(selected_request)
