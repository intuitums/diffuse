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
from pydantic import BaseModel

from service.model_capabilities import (
    EFFORT_LEVELS,
    REVIEW_DEPTHS,
    ReasoningMechanism,
    accepts,
    depth_for_effort,
    describe,
    effort_for_depth,
    plan_reasoning,
    plan_structured_output,
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


# --- Acceptance is not honouring, applied to a *different* parameter ---------


class _Probe(BaseModel):
    ready: bool


# Reaches structured output through a forced synthetic tool.
TOOL_MODE_MODEL = "anthropic/claude-sonnet-5"
# Same provider, but uses Anthropic's native `output_format` and renders no
# tools at all -- so there is nothing to un-force and nothing to repair.
NATIVE_FORMAT_MODEL = "anthropic/claude-sonnet-4-6"
# Passes `response_format` straight through; likewise no tools.
PASSTHROUGH_MODEL = "openai/gpt-5"


def test_a_depth_request_un_forces_structured_output_on_a_tool_route() -> None:
    """The finding, stated as a capability rather than a provider quirk.

    LiteLLM assigns the forcing `tool_choice` only when thinking is off, so
    asking for depth makes the JSON tool optional and the model may answer in
    prose. No probe of `reasoning_effort` alone can see this: the parameter is
    accepted and honoured, and it is `response_format` that quietly changes.
    """

    plan = plan_structured_output(
        TOOL_MODE_MODEL, _Probe, depth="careful", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.forced_without_depth is True
    assert plan.forced_with_depth is False
    assert plan.unforced_by_depth is True


def test_naming_the_tool_explicitly_restores_the_constraint() -> None:
    """Depth and forced structured output are not actually a trade-off."""

    plan = plan_structured_output(
        TOOL_MODE_MODEL, _Probe, depth="careful", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.repaired is True
    assert plan.tool_choice == {
        "type": "function",
        "function": {"name": "json_tool_call"},
    }


@pytest.mark.parametrize("depth", REVIEW_DEPTHS)
def test_every_depth_is_repaired_not_just_the_deep_ones(depth: str) -> None:
    """`brisk` enables thinking too, so it drops the forcing exactly the same."""

    plan = plan_structured_output(
        TOOL_MODE_MODEL, _Probe, depth=depth, max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.unforced_by_depth is True
    assert plan.repaired is True


@pytest.mark.parametrize("model", [NATIVE_FORMAT_MODEL, PASSTHROUGH_MODEL])
def test_routes_that_never_forced_a_tool_are_left_alone(model: str) -> None:
    """The blanket fix is a guaranteed 400 here, which is why this is a probe.

    These render no tools, so a `tool_choice` would force one the request does
    not carry -- breaking a route that has no problem to begin with.
    """

    plan = plan_structured_output(
        model, _Probe, depth="careful", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.forced_without_depth is False
    assert plan.repaired is False
    assert plan.tool_choice is None


def test_a_route_with_no_reasoning_control_needs_no_repair() -> None:
    """Nothing is sent to `gpt-4.1-mini`, so nothing can be un-forced."""

    plan = plan_structured_output(
        SAMPLING_ONLY_MODEL, _Probe, depth="exhaustive", max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.repaired is False


def test_no_depth_requested_leaves_the_forcing_where_it_was() -> None:
    """With no depth there is no interaction, and nothing to add."""

    plan = plan_structured_output(
        TOOL_MODE_MODEL, _Probe, depth=None, max_output_tokens=MAX_OUTPUT_TOKENS
    )

    assert plan.forced_without_depth is True
    assert plan.forced_with_depth is True
    assert plan.repaired is False


def test_an_unroutable_identifier_needs_no_repair() -> None:
    """A typo must not become an exception on the review hot path."""

    plan = plan_structured_output("not-a-real-provider/nope", _Probe, depth="careful")

    assert plan.repaired is False


@pytest.mark.parametrize("module", ["model_capabilities.py", "model_providers.py"])
def test_the_leaf_modules_stay_leaves(module: str) -> None:
    """`indexer` and `review.engine` both depend on these; a `service` import in
    either would make that a cycle.

    Read as source text this guard checked for the literal strings `from
    service` and `import service`, which is three ways short of the contract it
    states. `from .model_providers import ...` reaches the same module by a
    relative path, `importlib.import_module("service.x")` never spells an import
    at all, and `model_providers.py` -- which documents the identical contract
    and is the reason this one is written the way it is -- had no guard
    whatsoever. Asking the parse tree costs the same and answers the question
    that was being asked.
    """

    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "service" / module
    tree = ast.parse(path.read_text(), filename=str(path))

    def is_service(name: str | None) -> bool:
        return name == "service" or (name or "").startswith("service.")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not is_service(alias.name), (
                    f"{module} imports {alias.name}; it must stay a leaf."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"{module} uses a relative import, which reaches `service` "
                "without naming it."
            )
            assert not is_service(node.module), (
                f"{module} imports from {node.module}; it must stay a leaf."
            )
        elif isinstance(node, ast.Call):
            target = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if target not in {"import_module", "__import__"}:
                continue
            for argument in node.args:
                assert not (
                    isinstance(argument, ast.Constant) and is_service(argument.value)
                ), f"{module} imports {argument.value!r} dynamically; it must stay a leaf."
