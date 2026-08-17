"""Inputs an isolated Review Agent needs to produce a `ReviewReport`.

The request carries exact-head review context plus the durable candidate and
verifier dispatches. Historical model-name fields remain only as persisted
provenance labels; direct model API execution is retired.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repository_policy.resolve import ResolvedReviewPolicy
    from retriever.context_models import CrossRepositoryContextPlan
    from retriever.retrieve import RetrievedContext
    from service.review.agent_client import NativeSessionDispatch
    from service.review.tools import ReviewToolProvider


@dataclass(frozen=True)
class ReviewRequest:
    """Everything an Agent investigation may need; unused fields stay ``None``.

    The candidate reads the exact diff, policy, workspace artifact, and context
    plan. The worker callback mints the independent verifier only after accepting
    the candidate result. `candidate_model` and `verifier_model` are historical
    storage/provenance labels, not direct model routing inputs.
    """

    diff_text: str
    contexts: Sequence[RetrievedContext] = ()
    policy: ResolvedReviewPolicy | None = None
    progress_callback: Callable[[], None] | None = None
    candidate_model: str | None = None
    verifier_model: str | None = None
    worktree: Path | None = None
    context_plan: CrossRepositoryContextPlan | None = None
    tools: ReviewToolProvider | None = None
    #: Optional opaque identity for logging (review_run id as text, fixture id).
    review_identity: str | None = None
    agent_investigation: NativeSessionDispatch | None = None
    native_verifier_factory: (
        Callable[[str, dict[str, object], str], NativeSessionDispatch] | None
    ) = None

    def with_tools(self, tools: ReviewToolProvider) -> ReviewRequest:
        """Return a copy that carries `tools` without mutating this request."""

        return ReviewRequest(
            diff_text=self.diff_text,
            contexts=self.contexts,
            policy=self.policy,
            progress_callback=self.progress_callback,
            candidate_model=self.candidate_model,
            verifier_model=self.verifier_model,
            worktree=self.worktree,
            context_plan=self.context_plan,
            tools=tools,
            review_identity=self.review_identity,
            agent_investigation=self.agent_investigation,
            native_verifier_factory=self.native_verifier_factory,
        )
