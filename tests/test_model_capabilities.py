"""Capability resolution must come from LiteLLM, not from a table of names.

Everything here runs offline against the pinned `litellm==1.93.0` parameter
mapping, which is how W0.1 verified the two probes this module replaces. No test
needs a credential or makes a request.

Each capability shape is covered by a real model identifier rather than a mock,
because the point of the module is that the answer comes from LiteLLM: a fake
would only assert that the classifier can read a dictionary the test wrote.
"""

from __future__ import annotations

import pytest

from service.model_capabilities import (
    EFFORT_LEVELS,
    REVIEW_DEPTHS,
    ReasoningMechanism,
    accepts,
    depth_for_effort,
    describe,
    effort_for_depth,
    plan_reasoning,
)

# One model per mechanism, all four verified against the pinned LiteLLM.
EFFORT_SCALE_MODEL = "anthropic/claude-sonnet-5"
THINKING_BUDGET_MODEL = "anthropic/claude-haiku-4-5"
SWITCH_MODEL = "ollama/llama3"
SAMPLING_ONLY_MODEL = "openai/gpt-4.1-mini"

MAX_OUTPUT_TOKENS = 16000


def test_effort_scale_model_receives_the_graded_level() -> None:
    """Claude Sonnet 5 renders `reasoning_effort` as `output_config.effort`."""

    plan = plan_reasoning(EFFORT_SCALE_MODEL, "careful", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert plan.mechanism is ReasoningMechanism.EFFORT_SCALE
    assert plan.effort == "high"
    assert plan.exact
    assert plan.rendered["output_config"] == {"effort": "high"}


def test_thinking_budget_model_receives_a_token_budget_instead() -> None:
    """Haiku 4.5 renders the same request as `thinking.budget_tokens`.

    A different mechanism entirely, from the same provider and the same
    parameter -- which is why a per-parameter probe cannot answer this and a
    model-name table would have to be edited for every release.
    """

    plan = plan_reasoning(THINKING_BUDGET_MODEL, "careful", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert plan.mechanism is ReasoningMechanism.TOKEN_BUDGET
    assert plan.thinking_tokens == 4096
    assert plan.exact
    assert plan.rendered["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_sampling_only_model_reports_no_reasoning_control() -> None:
    """gpt-4.1-mini refuses `reasoning_effort` and accepts `temperature`."""

    plan = plan_reasoning(SAMPLING_ONLY_MODEL, "exhaustive", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert plan.mechanism is ReasoningMechanism.NONE
    assert plan.effort is None
    assert not plan.honored
    assert plan.rendered == {}
    assert accepts(SAMPLING_ONLY_MODEL, "temperature", 0.1)


def test_reasoning_rejecting_model_that_also_refuses_temperature() -> None:
    """Sonnet 5 is the inverse: reasoning yes, sampling no.

    The two axes are independent, so one probe cannot stand in for the other.
    """

    assert not accepts(EFFORT_SCALE_MODEL, "temperature", 0.1)
    assert accepts(EFFORT_SCALE_MODEL, "reasoning_effort", "high")


def test_a_switch_route_is_not_reported_as_a_graded_scale() -> None:
    """Ollama turns thinking on or off; the level itself is discarded.

    `plan.exact` is False even though the request was accepted, because the
    operator asked for a depth and the route cannot express one. Reporting this
    as success is the silent-drop failure wearing a different hat.
    """

    plan = plan_reasoning(SWITCH_MODEL, "careful", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert plan.mechanism is ReasoningMechanism.SWITCH
    assert plan.honored
    assert not plan.exact
    assert plan.rendered["think"] is True


def test_a_level_that_renders_thinking_off_is_stepped_down_not_sent() -> None:
    """Ollama renders `xhigh` and `max` as `think: false`.

    Asking for the most reasoning turns reasoning off. A probe that only asks
    "did LiteLLM raise?" reports this as honoured -- and `.env.example` used to
    recommend `xhigh` while listing `ollama/` as a route that accepts it.
    """

    from service.model_capabilities import _render

    assert _render(SWITCH_MODEL, "reasoning_effort", "xhigh", MAX_OUTPUT_TOKENS)["think"] is False

    plan = plan_reasoning(SWITCH_MODEL, "exhaustive", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert plan.rendered["think"] is True
    assert plan.effort == "high"


def test_a_thinking_budget_that_would_not_fit_is_stepped_down() -> None:
    """`exhaustive` on Haiku asks for 16384 thinking tokens inside 16000.

    Anthropic requires the thinking budget to sit strictly inside `max_tokens`,
    so LiteLLM renders a request the provider answers with a 400. Only a probe
    that reads the rendering *and* knows the output budget can see it.
    """

    plan = plan_reasoning(
        THINKING_BUDGET_MODEL, "exhaustive", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.thinking_tokens is not None
    assert plan.thinking_tokens < MAX_OUTPUT_TOKENS
    assert not plan.exact

    # And it is not a fixed ceiling: a larger output budget restores the level.
    roomy = plan_reasoning(THINKING_BUDGET_MODEL, "exhaustive", max_output_tokens=64000)
    assert roomy.thinking_tokens == 16384
    assert roomy.exact


def test_a_rejected_level_steps_down_to_the_deepest_the_route_accepts() -> None:
    """Gemini accepts low/medium/high and rejects xhigh and max outright."""

    plan = plan_reasoning(
        "gemini/gemini-2.5-pro", "exhaustive", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.mechanism is ReasoningMechanism.TOKEN_BUDGET
    assert plan.effort == "high"
    assert not plan.exact


def test_an_unroutable_identifier_resolves_to_no_capability() -> None:
    """A typo must not become an exception on the review hot path."""

    capabilities = describe("not-a-real-provider/nope", temperature=0.1)

    assert capabilities.routed is False
    assert capabilities.reasoning_mechanism is ReasoningMechanism.NONE
    assert capabilities.accepts_temperature is False


def test_describe_reports_both_axes_and_the_supported_parameter_list() -> None:
    """What `diffuse model` prints and `diffuse init` (W5.2) will show."""

    capabilities = describe(
        THINKING_BUDGET_MODEL, temperature=0.1, max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert capabilities.routed is True
    assert capabilities.accepts_temperature is True
    assert capabilities.supports_structured_output is True
    assert capabilities.reasoning_mechanism is ReasoningMechanism.TOKEN_BUDGET
    assert "reasoning_effort" in capabilities.supported_parameters
    assert capabilities.as_dict()["reasoning_mechanism"] == "token-budget"


@pytest.mark.parametrize("depth", REVIEW_DEPTHS)
def test_every_depth_round_trips_through_its_effort_rung(depth: str) -> None:
    assert depth_for_effort(effort_for_depth(depth)) == depth


def test_the_two_vocabularies_stay_the_same_length() -> None:
    """One rung per depth. A depth with no rung would resolve to nothing."""

    assert len(REVIEW_DEPTHS) == len(EFFORT_LEVELS)


def test_an_unknown_depth_is_refused_rather_than_approximated() -> None:
    with pytest.raises(ValueError, match="review depth must be one of"):
        plan_reasoning(EFFORT_SCALE_MODEL, "very-hard")


def test_the_module_stays_a_leaf() -> None:
    """`indexer` and `review_engine` both depend on this; a `service` import
    here would make that a cycle. `model_providers` carries the same contract
    and the same guard.
    """

    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "service" / "model_capabilities.py"
    ).read_text()

    assert "from service" not in source
    assert "import service" not in source
