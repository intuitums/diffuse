"""The runtime seam: selection, refusal, and dispatch.

None of these call a model. They cover the switch itself -- which runtime a
given `REVIEW_RUNTIME` selects, which ones a server process refuses, and that
`generate_review` delegates rather than reimplementing.
"""

import pytest

from repository_policy.resolve import ResolvedReviewPolicy
from retriever.retrieve import RetrievedContext
from service.models.review import ReviewReport
from service.review import engine as review_engine
from service.review import runtimes
from service.review.request import ReviewRequest
from service.review.runtimes import (
    CLAUDE_CODE_RUNTIME,
    CODEX_RUNTIME,
    LITELLM_RUNTIME,
    RUNTIME_NAMES,
    hosted_review_runtime_name,
    resolve_review_runtime,
    review_runtime_name,
)

#: A name `REVIEW_RUNTIME` will never accept, used to prove refusal.
UNSUPPORTED_RUNTIME = "unsupported-runtime"


class RecordingRuntime:
    """A runtime that records its call instead of reviewing anything."""

    def __init__(self, report: ReviewReport) -> None:
        self.report = report
        self.calls: list[ReviewRequest] = []

    @property
    def name(self) -> str:
        return "recording"

    def generate(self, request: ReviewRequest) -> ReviewReport:
        self.calls.append(request)
        return self.report


def _report() -> ReviewReport:
    return ReviewReport(
        summary="none",
        risk_score=0,
        confidence_score=0,
        findings=[],
        diff_file_count=0,
        reviewed_file_count=0,
        ignored_file_count=0,
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )


def test_unset_review_runtime_selects_litellm(monkeypatch):
    monkeypatch.delenv("REVIEW_RUNTIME", raising=False)
    assert review_runtime_name() == LITELLM_RUNTIME


def test_blank_review_runtime_selects_litellm(monkeypatch):
    """A variable set to whitespace is the shape a Compose file produces."""
    monkeypatch.setenv("REVIEW_RUNTIME", "   ")
    assert review_runtime_name() == LITELLM_RUNTIME


def test_unknown_review_runtime_is_refused(monkeypatch):
    """Deliberately not a near-miss of a planned name.

    `codex` and `claude` are the names the agent-CLI adapters will claim,
    so using either here -- or a plausible variant like `codex-cli` -- would
    read as "Codex is invalid" and would silently change meaning the day one of
    them is added to `RUNTIME_NAMES`. The value only has to be a name Diffuse
    will never accept.
    """
    monkeypatch.setenv("REVIEW_RUNTIME", UNSUPPORTED_RUNTIME)
    with pytest.raises(ValueError, match="is not a runtime Diffuse implements"):
        review_runtime_name()


def test_resolve_returns_the_litellm_runtime(monkeypatch):
    monkeypatch.delenv("REVIEW_RUNTIME", raising=False)
    runtime = resolve_review_runtime()
    assert isinstance(runtime, review_engine.LiteLLMRuntime)
    assert runtime.name == LITELLM_RUNTIME


def test_every_accepted_runtime_name_resolves(monkeypatch):
    """A name `REVIEW_RUNTIME` accepts must have an implementation behind it.

    `RUNTIME_NAMES` is the accept list. An entry without a branch in
    `resolve_review_runtime` would validate at startup and fail mid-review.
    """
    for name in RUNTIME_NAMES:
        monkeypatch.setenv("REVIEW_RUNTIME", name)
        assert resolve_review_runtime().name == name


def test_hosted_runtime_accepts_litellm(monkeypatch):
    monkeypatch.setenv("REVIEW_RUNTIME", LITELLM_RUNTIME)
    assert hosted_review_runtime_name() == LITELLM_RUNTIME


def test_hosted_runtime_accepts_both_isolated_native_runners(monkeypatch):
    for runtime in (CLAUDE_CODE_RUNTIME, CODEX_RUNTIME):
        monkeypatch.setenv("REVIEW_RUNTIME", runtime)
        assert hosted_review_runtime_name() == runtime
        assert resolve_review_runtime().name == runtime


def test_hosted_runtime_refuses_an_unsupported_runtime(monkeypatch):
    """Keep this test meaningful when a planned CLI runtime becomes hosted.

    `claude` and `codex` are intentional future values, so using either as a
    rejection fixture silently turned this test into a skip as soon as the
    adapter shipped. The sentinel is never a runtime, and proves that startup
    refuses a value the server cannot execute without constraining a future
    product decision.
    """
    monkeypatch.setenv("REVIEW_RUNTIME", UNSUPPORTED_RUNTIME)
    with pytest.raises(ValueError, match="is not a runtime Diffuse implements"):
        hosted_review_runtime_name()


def test_generate_review_delegates_every_argument(monkeypatch):
    """The dispatcher forwards, and does not review anything itself.

    Every argument is a distinguishable value rather than a default, so a
    dispatcher that silently dropped one fails here. `policy` is the case that
    motivates it: passed as `None` it is indistinguishable from not being
    forwarded at all, and it is the argument that decides which files a review
    is even allowed to comment on.
    """

    runtime = RecordingRuntime(_report())
    policy = ResolvedReviewPolicy(source_fingerprint="source", fingerprint="resolved", paths=())
    contexts = [
        RetrievedContext(
            file_path="app.py",
            symbol_name="handler",
            start_line=1,
            end_line=2,
            content="def handler():\n    return None\n",
            retrieval_reason="callers",
        )
    ]

    def progress() -> None:
        return None

    report = review_engine.generate_review(
        "diff --git a/app.py b/app.py\n",
        contexts,
        progress_callback=progress,
        policy=policy,
        candidate_model="anthropic/claude-sonnet-5",
        verifier_model="openai/gpt-5",
        runtime=runtime,
    )

    assert report is runtime.report
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call.diff_text == "diff --git a/app.py b/app.py\n"
    assert list(call.contexts) == contexts
    assert call.progress_callback is progress
    assert call.policy is policy
    assert call.candidate_model == "anthropic/claude-sonnet-5"
    assert call.verifier_model == "openai/gpt-5"


def test_generate_review_accepts_an_explicit_request(monkeypatch):
    runtime = RecordingRuntime(_report())
    request = ReviewRequest(
        diff_text="from-request",
        contexts=(),
        candidate_model="anthropic/claude-sonnet-5",
        worktree=None,
    )
    report = review_engine.generate_review(
        "ignored",
        [],
        runtime=runtime,
        request=request,
    )
    assert report is runtime.report
    assert runtime.calls[0] is request


def test_generate_review_resolves_the_configured_runtime(monkeypatch):
    """With no explicit runtime, the dispatcher reads `REVIEW_RUNTIME`."""
    runtime = RecordingRuntime(_report())
    monkeypatch.setattr(review_engine, "resolve_review_runtime", lambda: runtime)
    report = review_engine.generate_review("", [])
    assert report is runtime.report
    assert len(runtime.calls) == 1


def test_generate_review_does_not_resolve_twice_when_given_a_runtime(monkeypatch):
    """An explicit runtime wins, and configuration is not consulted at all."""

    def explode() -> None:
        raise AssertionError("configuration was read despite an explicit runtime")

    monkeypatch.setattr(review_engine, "resolve_review_runtime", explode)
    runtime = RecordingRuntime(_report())
    assert review_engine.generate_review("", [], runtime=runtime) is runtime.report


def test_litellm_runtime_calls_the_one_shot_body(monkeypatch):
    """`LiteLLMRuntime.generate` is a pass-through to the extracted body."""
    seen: dict = {}

    def fake_body(diff_text, contexts, **kwargs):
        seen["diff_text"] = diff_text
        seen["contexts"] = contexts
        seen.update(kwargs)
        return _report()

    monkeypatch.setattr(review_engine, "_generate_review_litellm", fake_body)
    policy = ResolvedReviewPolicy(source_fingerprint="source", fingerprint="resolved", paths=())
    runtime = review_engine.LiteLLMRuntime()
    runtime.generate(
        ReviewRequest(
            diff_text="diff",
            contexts=(),
            policy=policy,
            candidate_model="a",
            verifier_model="b",
        )
    )
    assert seen["diff_text"] == "diff"
    assert seen["policy"] is policy
    assert seen["candidate_model"] == "a"
    assert seen["verifier_model"] == "b"


def test_runtime_variable_name_is_stable():
    """Documented in `.env.example`; a rename is an operator-visible break."""
    assert runtimes.RUNTIME_VARIABLE == "REVIEW_RUNTIME"
