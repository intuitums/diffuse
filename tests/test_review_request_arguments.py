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

from service.review_engine import REVIEW_TEMPERATURE, verify_model_connection

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
