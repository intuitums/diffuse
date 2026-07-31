"""Review depth is an intent, and a depth that cannot be honoured is reported.

Two things are being defended here.

The first is that Diffuse's configuration states *how carefully to review* and
nothing about a provider's parameter names, because there is no single provider
parameter to state: the same `REVIEW_DEPTH=careful` has to become a graded
effort level, a thinking-token budget, an on/off switch, or nothing at all
depending on the model the operator named.

The second is that "or nothing at all" is never silent. Diffuse has no
structured logging, no metrics, and no alerting, so a parameter dropped mid-run
with a warning is indistinguishable from a parameter that was never asked for.
An operator who configures deep reasoning and quietly gets none is the failure
this unit exists to delete.

Everything runs offline against LiteLLM's parameter mapping. No credential, no
request.
"""

from __future__ import annotations

import logging

import litellm
import pytest

from service import worker
from service.model_capabilities import is_known_route, plan_reasoning
from service.review_engine import (
    REVIEW_TEMPERATURE,
    resolve_review_depth_support,
    review_depth,
    verify_model_connection,
)
from service.review_provenance import PullRequestProvenance, select_review_model_plan

EFFORT_SCALE_MODEL = "anthropic/claude-sonnet-5"
THINKING_BUDGET_MODEL = "anthropic/claude-haiku-4-5"
SAMPLING_ONLY_MODEL = "openai/gpt-4.1-mini"
# The spelling `.env.example` documents for an OpenAI-compatible server reached
# through REVIEW_API_BASE. LiteLLM routes it and knows nothing else about it.
SELF_HOSTED_MODEL = "openai/qwen3-32b"


class _Captured(Exception):
    """Stops the call once the provider arguments are known."""


@pytest.fixture(autouse=True)
def _clean_depth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_DEPTH", raising=False)
    monkeypatch.delenv("REVIEW_EFFORT", raising=False)
    monkeypatch.delenv("REVIEW_VERIFIER_MODEL", raising=False)
    monkeypatch.delenv("REVIEW_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("REVIEW_API_BASE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


def _request_arguments(monkeypatch: pytest.MonkeyPatch, model: str) -> dict[str, object]:
    """The arguments `_call_structured` would hand to LiteLLM for `model`."""

    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Captured

    monkeypatch.setattr(litellm, "completion", capture)
    with pytest.raises(_Captured):
        verify_model_connection(model)
    return captured


# --- The intent itself -------------------------------------------------------


def test_unset_depth_sends_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """No implicit default. An unset intent leaves the model entirely alone.

    Choosing a depth for the operator changes the bill and the latency of every
    review, which is precisely the guess Phase 0 deleted for the model itself.
    """

    assert review_depth() is None
    arguments = _request_arguments(monkeypatch, EFFORT_SCALE_MODEL)
    assert "reasoning_effort" not in arguments
    assert "thinking" not in arguments
    assert "output_config" not in arguments


def test_an_intent_word_reaches_a_graded_route_as_a_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    assert _request_arguments(monkeypatch, EFFORT_SCALE_MODEL)["reasoning_effort"] == "high"


def test_the_same_intent_word_reaches_a_budget_route_as_a_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same configuration, same provider, different mechanism entirely."""

    monkeypatch.setenv("REVIEW_MODEL", THINKING_BUDGET_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")
    monkeypatch.setenv("REVIEW_MAX_OUTPUT_TOKENS", "16000")

    (_stage, plan), = resolve_review_depth_support().plans

    # Stepped down from `max`, whose 16384-token thinking budget does not fit
    # inside the 16000-token output budget and would be answered with a 400.
    assert plan.mechanism.value == "token-budget"
    assert plan.effort == "xhigh"
    assert plan.thinking_tokens == 8192


def test_the_budget_a_route_derives_is_kept_inside_the_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`diffuse model --live` sends a 2048-token probe on purpose.

    A thinking budget is only valid strictly inside `max_tokens`, and LiteLLM
    renders `xhigh` as 8192 whatever the output budget is. Sending that on the
    connectivity probe reports a broken connection to an operator whose setup is
    fine -- so the resolution is per call, against the budget that call will use.
    """

    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")
    arguments = _request_arguments(monkeypatch, THINKING_BUDGET_MODEL)

    assert arguments["max_tokens"] == 2048
    assert arguments["reasoning_effort"] == "low"
    assert arguments["temperature"] == REVIEW_TEMPERATURE


def test_an_intent_word_is_not_sent_to_a_route_that_has_no_reasoning_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review still runs; the startup check is what refuses to be silent."""

    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")
    arguments = _request_arguments(monkeypatch, SAMPLING_ONLY_MODEL)

    assert "reasoning_effort" not in arguments
    assert arguments["temperature"] == REVIEW_TEMPERATURE


def test_a_typo_in_the_intent_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_DEPTH", "very careful")

    with pytest.raises(ValueError, match="REVIEW_DEPTH must be one of"):
        review_depth()


def test_the_previous_spelling_still_configures_the_same_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`REVIEW_EFFORT=high` and `REVIEW_DEPTH=careful` are the same rung."""

    monkeypatch.setenv("REVIEW_EFFORT", "high")
    assert review_depth() == "careful"


def test_setting_both_spellings_is_refused_rather_than_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently preferring one would hide the other from the operator."""

    monkeypatch.setenv("REVIEW_DEPTH", "brisk")
    monkeypatch.setenv("REVIEW_EFFORT", "max")

    with pytest.raises(ValueError) as failure:
        review_depth()

    message = str(failure.value)
    assert "REVIEW_DEPTH" in message
    assert "REVIEW_EFFORT" in message


# --- The startup report ------------------------------------------------------


def test_no_report_at_all_when_no_depth_was_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_MODEL", SAMPLING_ONLY_MODEL)

    support = resolve_review_depth_support()

    assert support.depth is None
    assert support.report_lines() == ()
    assert support.refusal() is None


def test_a_candidate_that_cannot_reason_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adjudicated case: an explicit request the model cannot honour.

    This used to be a `LOGGER.warning` on a hot path that runs once per model
    call, per pass, per pull request -- so the operator saw either nothing or a
    wall of identical lines, and in both cases got a shallow review they had
    paid for a deep one. It is now a refusal to start, naming the variable, the
    model, and what would actually have been sent.
    """

    monkeypatch.setenv("REVIEW_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")

    with pytest.raises(ValueError) as failure:
        worker.validate_worker_model_controls()

    message = str(failure.value)
    assert "REVIEW_DEPTH=exhaustive" in message
    assert SAMPLING_ONLY_MODEL in message
    assert "NOTHING will be sent" in message
    # Actionable: both ways out are named.
    assert "REVIEW_MODEL" in message
    assert "unset REVIEW_DEPTH" in message


def test_the_refusal_names_the_variable_the_operator_actually_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator on the older spelling must not be told to unset a variable
    they never set."""

    monkeypatch.setenv("REVIEW_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_EFFORT", "max")

    with pytest.raises(ValueError, match="REVIEW_EFFORT=exhaustive"):
        worker.validate_worker_model_controls()


def test_a_verifier_that_cannot_reason_is_reported_but_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.env.example` recommends a cross-family verifier, and the documented
    pairing (`openai/gpt-4.1-mini`) has no reasoning control at all. Refusing
    here would make review depth and cross-family verification mutually
    exclusive, so this reports instead -- loudly, and at startup.
    """

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    worker.validate_worker_model_controls()

    report = capsys.readouterr().err
    assert "candidate" in report
    assert "verifier" in report
    assert "NOTHING will be sent" in report


def test_an_unhonored_depth_is_reported_whatever_log_level_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`LOG_LEVEL` is documented in both env files, and the worker configures
    logging from it. Reporting the one thing an operator cannot otherwise
    discover through a channel they are invited to turn off means `exhaustive`
    silently becoming `high` produces no output at all on a supported
    configuration. So the unhonoured case does not go through logging.
    """

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")
    logging.disable(logging.CRITICAL)
    try:
        worker.validate_worker_model_controls()
    finally:
        logging.disable(logging.NOTSET)

    assert "NOTHING will be sent" in capsys.readouterr().err


def test_a_candidate_and_verifier_with_different_mechanisms_resolve_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request, two models, two different renderings of the same intent."""

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", THINKING_BUDGET_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    plans = dict(resolve_review_depth_support().plans)

    assert plans["candidate"].mechanism.value == "effort-scale"
    assert plans["verifier"].mechanism.value == "token-budget"
    assert plans["verifier"].thinking_tokens == 4096


def test_an_identical_candidate_and_verifier_are_reported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verifier defaults to the candidate; two identical lines read as two
    independent findings that happen to agree."""

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    support = resolve_review_depth_support()

    assert [stage for stage, _plan in support.plans] == ["candidate and verifier"]


def test_the_report_names_what_was_asked_and_what_will_be_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Requested, supported, sent" -- all three, or the report is not one."""

    monkeypatch.setenv("REVIEW_MODEL", THINKING_BUDGET_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")
    monkeypatch.setenv("REVIEW_MAX_OUTPUT_TOKENS", "16000")

    report = "\n".join(resolve_review_depth_support().report_lines())

    assert "REVIEW_DEPTH=exhaustive" in report
    assert "NOT AS REQUESTED" in report
    assert "reasoning_effort=xhigh" in report
    assert "8192 tokens" in report


def test_an_honored_request_is_reported_without_alarm(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A depth that is honoured exactly still says so, at INFO, once."""

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    with caplog.at_level(logging.INFO, logger="service.worker"):
        worker.validate_worker_model_controls()

    assert resolve_review_depth_support().fully_honored
    assert caplog.records
    assert all(record.levelno == logging.INFO for record in caplog.records)


def test_startup_validation_runs_the_model_control_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check nothing calls is not a check. Guards the wiring in `main` and,
    because startup only ever sees the configured pair, in the review path that
    resolves the routed one."""

    import inspect

    assert "validate_worker_model_controls()" in inspect.getsource(worker.main)
    routed = inspect.getsource(worker.process_review_job)
    assert "resolve_review_depth_support(" in routed
    assert "model_plan.candidate_model" in routed


# --- The pair that actually reviews -------------------------------------------


def test_provenance_routing_resolves_depth_for_the_pair_it_chose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configuration `.env.example` recommends, on the pull requests Diffuse
    is aimed at, reviews with no reasoning depth at all.

    `select_review_model_plan` permutes the configured pair when provenance is
    confident, so an AI-authored change makes the *verifier* the candidate. With
    the documented cross-family verifier that candidate is
    `openai/gpt-4.1-mini`, which has no reasoning control -- so startup's
    resolution of the configured pair says `exhaustive` is honoured while the
    pass that actually runs sends nothing. Resolving only the configured pair is
    what let that through.
    """

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")

    configured = resolve_review_depth_support()
    assert dict(configured.plans)["candidate"].honored

    plan = select_review_model_plan(
        PullRequestProvenance(
            classification="agent_authored",
            model_family="anthropic",
            tool="claude-code",
            confidence=0.98,
            commit_count=1,
            ai_commit_count=1,
            metadata_complete=True,
            signals=(),
        ),
        candidate_model=EFFORT_SCALE_MODEL,
        verifier_model=SAMPLING_ONLY_MODEL,
    )
    assert plan.candidate_model == SAMPLING_ONLY_MODEL

    routed = resolve_review_depth_support(
        candidate_model=plan.candidate_model,
        verifier_model=plan.verifier_model,
        source=f"routed by provenance: {plan.reason_code}",
    )

    assert not dict(routed.plans)["candidate"].honored
    report = "\n".join(routed.report_lines())
    assert "routed by provenance: opposing_anthropic_reviewer" in report
    assert "NOTHING will be sent" in report


def test_the_routed_resolution_is_recorded_with_the_review_it_applied_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log line outlives nothing and `LOG_LEVEL` can delete it, so what the
    models were actually asked to do is stored on the run."""

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")

    summary = resolve_review_depth_support(
        candidate_model=SAMPLING_ONLY_MODEL,
        verifier_model=EFFORT_SCALE_MODEL,
        source="routed by provenance: opposing_anthropic_reviewer",
    ).summary()

    assert summary is not None
    assert "REVIEW_DEPTH=exhaustive" in summary
    assert "opposing_anthropic_reviewer" in summary
    assert SAMPLING_ONLY_MODEL in summary
    assert "NOTHING will be sent" in summary
    # The column is bounded, and an over-long value would fail the whole review.
    assert len(summary.encode()) <= 8192


def test_no_depth_requested_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset depth has no resolution to record; the column stays empty."""

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)

    assert resolve_review_depth_support(
        candidate_model=SAMPLING_ONLY_MODEL,
        verifier_model=SAMPLING_ONLY_MODEL,
    ).summary() is None


# --- What the probe does not know is not a finding ----------------------------


def test_a_self_hosted_deployment_is_reported_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.env.example` documents both of these spellings for an OpenAI-compatible
    server behind `REVIEW_API_BASE`, and LiteLLM has no metadata for either. An
    empty rendering there is an absence of knowledge, not evidence that the
    model has no reasoning control -- the same server named `hosted_vllm/`
    renders a graded effort. Refusing to start on it asserted something the
    probe cannot know.
    """

    monkeypatch.setenv("REVIEW_MODEL", SELF_HOSTED_MODEL)
    monkeypatch.setenv("REVIEW_API_BASE", "http://vllm:8000")
    monkeypatch.setenv("REVIEW_DEPTH", "thorough")

    support = resolve_review_depth_support()

    assert not dict(support.plans)["candidate and verifier"].known_route
    assert support.refusal() is None
    worker.validate_worker_model_controls()
    assert "no metadata" in capsys.readouterr().err


def test_the_same_server_named_by_its_own_prefix_honors_the_depth() -> None:
    """The disproof, measured rather than asserted: one deployment, two
    spellings, and only one of them used to start."""

    plan = plan_reasoning("hosted_vllm/qwen3-32b", "thorough", max_output_tokens=16000)

    assert plan.honored
    assert plan.effort == "xhigh"


def test_a_model_litellm_knows_has_no_control_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relaxation above must not become a hole. When LiteLLM does hold
    metadata, `NONE` is a positive finding and startup still stops."""

    monkeypatch.setenv("REVIEW_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")

    assert is_known_route(SAMPLING_ONLY_MODEL)
    with pytest.raises(ValueError, match="no reasoning control"):
        worker.validate_worker_model_controls()


# --- Naming the variable that is actually binding -----------------------------


def test_an_output_budget_that_blocks_every_rung_names_that_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At `REVIEW_MAX_OUTPUT_TOKENS=1024` every thinking budget
    `anthropic/claude-haiku-4-5` renders is at least 1024, so no rung survives
    and the route reports no mechanism -- while the model's reasoning control is
    in perfect working order. Saying "this model has no reasoning control, set
    REVIEW_MODEL to one that has" is false in both halves, and following it
    leaves the operator with a second model that also cannot reason at 1024.
    """

    monkeypatch.setenv("REVIEW_MODEL", THINKING_BUDGET_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")
    monkeypatch.setenv("REVIEW_MAX_OUTPUT_TOKENS", "1024")

    with pytest.raises(ValueError) as failure:
        worker.validate_worker_model_controls()

    message = str(failure.value)
    assert "REVIEW_MAX_OUTPUT_TOKENS=1024" in message
    assert "Raise REVIEW_MAX_OUTPUT_TOKENS" in message
    assert "binding constraint here, not the model" in message
    assert "no reasoning control" not in message


def test_a_roomier_output_budget_leaves_the_same_model_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the diagnosis: the remedy the message names works."""

    monkeypatch.setenv("REVIEW_MODEL", THINKING_BUDGET_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")
    monkeypatch.setenv("REVIEW_MAX_OUTPUT_TOKENS", "16000")

    support = resolve_review_depth_support()

    assert support.refusal() is None
    assert dict(support.plans)["candidate and verifier"].thinking_tokens == 4096
