"""The request Diffuse actually sends must be one its documented models accept.

Every other review test replaces `_call_structured`, so nothing exercised the
provider arguments themselves. That gap shipped a hardcoded `temperature` the
recommended model refuses, which LiteLLM rejects client-side as a
`UnsupportedParamsError` -- a `BadRequestError`, not a `ValueError`, so
`worker.run_once` classified it as retryable: five attempts and a failure
comment on every pull request.

These tests capture the argument dictionary `_call_structured` builds and put
**the whole of it** through LiteLLM's parameter mapping. Reconstructing a
hand-picked subset here is what reopened the original blind spot once already:
an earlier version of this file forwarded only the sampling parameters and
dropped `response_format`, which `_call_structured` always adds on a route that
supports a JSON schema -- and which is exactly the parameter whose interaction
with the others decides whether structured output is *forced* or merely
offered. Anything the request carries has to be rendered here, or the rendering
under test is not the one the provider sees.
"""

import inspect

import litellm
import pytest
from litellm.utils import get_optional_params

from service.model_capabilities import REVIEW_DEPTHS
from service.review.engine import (
    REVIEW_EFFORT_LEVELS,
    REVIEW_TEMPERATURE,
    verify_model_connection,
)

# Every model the env files tell an operator to configure, including the one
# they recommend and the "maximum depth" upgrade beside it.
DOCUMENTED_MODELS = (
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4.1-mini",
)

# `_call_structured` hands these to the LiteLLM client rather than to a
# provider's parameter mapping: they select the route, authenticate it, and
# bound the call. Everything else shapes the request body and must be rendered.
TRANSPORT_ARGUMENTS = frozenset({"model", "api_key", "api_base", "timeout", "num_retries"})

# Rendered from `response_format` by the `anthropic/` routes: a `json_tool_call`
# tool the model is *required* to use. Losing the `tool_choice` while keeping
# the tool leaves the model free to answer in prose, which
# `model_validate_json` rejects -- a `StructuredOutputValidationError`, which is
# retryable, so it costs five attempts and a terminal failure notice on the pull
# request rather than failing once and visibly.
FORCED_JSON_TOOL = {"name": "json_tool_call", "type": "tool"}


class _Captured(Exception):
    """Stops the call once the provider arguments are known."""


def _request_arguments(monkeypatch: pytest.MonkeyPatch, model: str) -> dict[str, object]:
    """Return the arguments `_call_structured` would hand to LiteLLM."""

    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Captured

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    # Every variable that changes the shape of the captured request, cleared so
    # a developer who happens to have one exported does not silently test a
    # different request than CI does. REVIEW_STRUCTURED_OUTPUT_MODE matters
    # most: `prompt` removes `response_format` altogether and would make the
    # rendering assertions below vacuous.
    for name in (
        "REVIEW_API_BASE",
        "REVIEW_MAX_OUTPUT_TOKENS",
        "REVIEW_MODEL_TIMEOUT_SECONDS",
        "REVIEW_MODEL_RETRIES",
        "REVIEW_STRUCTURED_OUTPUT_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(litellm, "completion", capture)
    with pytest.raises(_Captured):
        verify_model_connection(model)
    return captured


def _rendered_parameters(arguments: dict[str, object]) -> dict[str, object]:
    """Every request-shaping argument, through LiteLLM's provider mapping.

    Raises `UnsupportedParamsError` if the provider refuses any of them, and
    otherwise returns what the provider is actually sent -- which is the only
    place an interaction between two individually-accepted parameters is
    visible.
    """

    resolved, provider, _, _ = litellm.get_llm_provider(model=str(arguments["model"]))
    shaping = {name: value for name, value in arguments.items() if name not in TRANSPORT_ARGUMENTS}
    unknown = set(shaping) - set(inspect.signature(get_optional_params).parameters)
    assert unknown == set(), (
        f"_call_structured now sends {sorted(unknown)}, which LiteLLM's parameter "
        "mapping does not name. Either it is transport (add it to "
        "TRANSPORT_ARGUMENTS) or this test is no longer rendering the whole request."
    )
    return get_optional_params(
        model=resolved,
        custom_llm_provider=provider,
        **shaping,
    )


@pytest.mark.parametrize("model", DOCUMENTED_MODELS)
def test_documented_models_accept_the_request_diffuse_sends(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    _rendered_parameters(_request_arguments(monkeypatch, model))


@pytest.mark.parametrize(
    "model", [name for name in DOCUMENTED_MODELS if name.startswith("anthropic/")]
)
def test_structured_output_stays_forced_on_the_anthropic_routes(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """A request that offers the JSON tool without requiring it is a bug.

    `AnthropicConfig.map_openai_params` forces the tool only when thinking is
    not enabled, so any parameter that turns thinking on -- `reasoning_effort`
    is one -- silently removes the `tool_choice` while leaving the tool and
    `json_mode` in place. Nothing raises; the model simply becomes free to
    answer in prose. Rendering the whole request is what makes that visible.
    """

    rendered = _rendered_parameters(_request_arguments(monkeypatch, model))

    assert rendered["tool_choice"] == FORCED_JSON_TOOL


def test_temperature_is_omitted_for_models_that_refuse_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sonnet 5, Opus 4.7/4.8, and Fable 5 removed the sampling parameters."""

    assert "temperature" not in _request_arguments(monkeypatch, "anthropic/claude-opus-4-8")


def test_temperature_is_sent_where_it_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping it everywhere would silently loosen self-hosted and OpenAI routes."""

    arguments = _request_arguments(monkeypatch, "openai/gpt-4.1-mini")
    assert arguments["temperature"] == REVIEW_TEMPERATURE


def test_connection_probe_leaves_room_for_a_thinking_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that truncates reports a broken connection to a working setup."""

    arguments = _request_arguments(monkeypatch, "anthropic/claude-sonnet-5")
    assert int(arguments["max_tokens"]) >= 1024


def test_unset_effort_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset REVIEW_EFFORT must leave the model on its own default."""

    monkeypatch.delenv("REVIEW_EFFORT", raising=False)
    assert "reasoning_effort" not in _request_arguments(
        monkeypatch, "anthropic/claude-sonnet-5"
    )


@pytest.mark.parametrize("effort", REVIEW_EFFORT_LEVELS)
def test_every_documented_effort_reaches_an_anthropic_model(
    monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    """REVIEW_EFFORT maps to `output_config.effort` through LiteLLM's mapping."""

    monkeypatch.setenv("REVIEW_EFFORT", effort)
    arguments = _request_arguments(monkeypatch, "anthropic/claude-sonnet-5")
    assert arguments["reasoning_effort"] == effort

    resolved, provider, _, _ = litellm.get_llm_provider(model="anthropic/claude-sonnet-5")
    mapped = get_optional_params(
        model=resolved,
        custom_llm_provider=provider,
        max_tokens=arguments["max_tokens"],
        reasoning_effort=effort,
    )
    assert mapped["output_config"] == {"effort": effort}


def test_effort_is_dropped_rather_than_failing_a_model_that_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-4.1-mini has no reasoning_effort; a review must still run."""

    monkeypatch.setenv("REVIEW_EFFORT", "xhigh")
    arguments = _request_arguments(monkeypatch, "openai/gpt-4.1-mini")
    assert "reasoning_effort" not in arguments
    # And the parameter it does honor is still there.
    assert arguments["temperature"] == REVIEW_TEMPERATURE


def test_an_unknown_effort_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must fail loudly at configuration time, not silently do nothing."""

    monkeypatch.setenv("REVIEW_EFFORT", "maximum")
    with pytest.raises(ValueError, match="REVIEW_EFFORT"):
        _request_arguments(monkeypatch, "anthropic/claude-sonnet-5")


# --- Structured output must survive a depth request --------------------------
#
# Reconstructing the call from a hand-picked subset of parameters is exactly the
# blind spot that let this through: `response_format` was dropped before the
# rendering, and it is the parameter whose interaction with `reasoning_effort`
# breaks. `_rendered_parameters` above renders the whole request, so the depth
# tests below read through it under a shorter name.

# The two documented routes that reach structured output through a forced tool.
# `openai/gpt-4.1-mini` is excluded deliberately -- it has no reasoning control,
# so there is no combination to test.
TOOL_MODE_MODELS = ("anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5")

_rendered = _rendered_parameters


@pytest.mark.parametrize("model", TOOL_MODE_MODELS)
@pytest.mark.parametrize("depth", REVIEW_DEPTHS)
def test_structured_output_stays_forced_at_every_review_depth(
    monkeypatch: pytest.MonkeyPatch, model: str, depth: str
) -> None:
    """Asking for depth must not make the JSON tool optional.

    `AnthropicConfig.map_openai_params` assigns the forcing `tool_choice` only
    `if not is_thinking_enabled`, so any depth request silently removed it and
    the model was free to answer in prose. `model_validate_json` then raises
    `StructuredOutputValidationError`, which is *retryable* -- five attempts and
    a terminal failure notice on a pull request, which is the exact failure
    shape Phase 0 exists to delete, on the pairing `.env.example` recommends.
    """

    monkeypatch.setenv("REVIEW_DEPTH", depth)
    rendered = _rendered(_request_arguments(monkeypatch, model))

    assert rendered.get("json_mode") is True
    tools = [tool.get("name") for tool in rendered.get("tools", [])]
    assert tools, f"{model} at depth {depth} renders no JSON tool to force"
    assert rendered.get("tool_choice") is not None, (
        f"{model} at depth {depth} renders the JSON tool but does not force it, "
        "so the model may answer in prose instead of calling it."
    )
    assert rendered["tool_choice"].get("name") in tools


@pytest.mark.parametrize("model", TOOL_MODE_MODELS)
def test_depth_is_still_requested_alongside_forced_structured_output(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """Keeping the tool forced must not be paid for by dropping the depth.

    The cheap way to fix the above is to stop sending depth on these routes.
    That trades one silent downgrade for another, so pin both halves.
    """

    monkeypatch.setenv("REVIEW_DEPTH", "careful")
    rendered = _rendered(_request_arguments(monkeypatch, model))

    reasoning = rendered.get("output_config") or rendered.get("thinking")
    assert reasoning, f"{model} lost its reasoning request while forcing the tool"
    assert rendered.get("tool_choice") is not None


def test_no_tool_choice_is_invented_for_routes_that_never_forced_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair is narrow on purpose, and the blanket version is a 400.

    `openai/`, `hosted_vllm/` and `gemini/` pass `response_format` through and
    render no tools at all, as do the `anthropic/` models that use the native
    `output_format`. Sending a `tool_choice` there forces a tool the request
    does not carry -- turning a route that works today into a hard failure.
    """

    monkeypatch.setenv("REVIEW_DEPTH", "careful")

    for model in ("openai/gpt-5", "anthropic/claude-sonnet-4-6"):
        arguments = _request_arguments(monkeypatch, model)
        assert "tool_choice" not in arguments, (
            f"{model} renders no tools, so forcing one would be a 400"
        )


def test_a_route_with_no_reasoning_control_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No depth reaches gpt-4.1-mini, so nothing needs repairing there."""

    monkeypatch.setenv("REVIEW_DEPTH", "exhaustive")
    arguments = _request_arguments(monkeypatch, "openai/gpt-4.1-mini")

    assert "reasoning_effort" not in arguments
    assert "tool_choice" not in arguments
