"""The inputs a review runtime needs to produce a `ReviewReport`.

Callers historically passed a flat argument list shaped for the one-shot API
runtime (diff + pre-fused contexts + models). Agent-CLI runtimes need a
worktree, a context plan, and tools instead of a preloaded blob. `ReviewRequest`
is that richer envelope; `generate_review` still accepts the flat form and
builds one so existing callers do not all move at once.
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
    from service.review.native_runner import NativeSessionDispatch
    from service.review.tools import ReviewToolProvider


@dataclass(frozen=True)
class ReviewRequest:
    """Everything a runtime may need; unused fields stay None.

    The one-shot API runtime reads `diff_text`, `contexts`, `policy`, and the
    model pair. An agent-CLI runtime reads `diff_text`, `policy`, `worktree`,
    `context_plan`, and `tools`, and ignores preloaded `contexts` except as a
    hint. Provenance routing still supplies `candidate_model` /
    `verifier_model`; a runtime that does not use API models leaves them unset.
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
    agent_session: NativeSessionDispatch | None = None

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
            agent_session=self.agent_session,
        )
