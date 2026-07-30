"""The request Diffuse actually sends must be one its documented models accept.

Every other review test replaces `_call_structured`, so nothing exercised the
provider arguments themselves. That gap shipped a hardcoded `temperature` the
recommended model refuses, which LiteLLM rejects client-side as a
`UnsupportedParamsError` -- a `BadRequestError`, not a `ValueError`, so
`worker.run_once` classified it as retryable: five attempts and a failure
comment on every pull request. These tests build the real argument dictionary
and put it through LiteLLM's parameter mapping.
"""

import litellm
import pytest
from litellm.utils import get_optional_params

from service.review_engine import (
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

SAMPLING_PARAMETERS = ("temperature", "top_p", "top_k", "reasoning_effort")


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
    monkeypatch.delenv("REVIEW_API_BASE", raising=False)
    monkeypatch.delenv("REVIEW_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setattr(litellm, "completion", capture)
    with pytest.raises(_Captured):
        verify_model_connection(model)
    return captured


@pytest.mark.parametrize("model", DOCUMENTED_MODELS)
def test_documented_models_accept_the_request_diffuse_sends(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    arguments = _request_arguments(monkeypatch, model)
    resolved, provider, _, _ = litellm.get_llm_provider(model=str(arguments["model"]))
    sampling = {name: arguments[name] for name in SAMPLING_PARAMETERS if name in arguments}

    # Raises UnsupportedParamsError if the provider refuses any of them.
    get_optional_params(
        model=resolved,
        custom_llm_provider=provider,
        max_tokens=arguments["max_tokens"],
        **sampling,
    )


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
