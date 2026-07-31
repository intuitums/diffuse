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
from service.review_engine import (
    REVIEW_TEMPERATURE,
    resolve_review_depth_support,
    review_depth,
    verify_model_connection,
)

EFFORT_SCALE_MODEL = "anthropic/claude-sonnet-5"
THINKING_BUDGET_MODEL = "anthropic/claude-haiku-4-5"
SAMPLING_ONLY_MODEL = "openai/gpt-4.1-mini"


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
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`.env.example` recommends a cross-family verifier, and the documented
    pairing (`openai/gpt-4.1-mini`) has no reasoning control at all. Refusing
    here would make review depth and cross-family verification mutually
    exclusive, so this reports instead -- loudly, and at startup.
    """

    monkeypatch.setenv("REVIEW_MODEL", EFFORT_SCALE_MODEL)
    monkeypatch.setenv("REVIEW_VERIFIER_MODEL", SAMPLING_ONLY_MODEL)
    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    with caplog.at_level(logging.WARNING, logger="service.worker"):
        worker.validate_worker_model_controls()

    report = "\n".join(record.getMessage() for record in caplog.records)
    assert "candidate" in report
    assert "verifier" in report
    assert "NOTHING will be sent" in report
    assert all(record.levelno >= logging.WARNING for record in caplog.records)


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
    """A check nothing calls is not a check. Guards the wiring in `main`."""

    import inspect

    source = inspect.getsource(worker.main)
    assert "validate_worker_model_controls()" in source
