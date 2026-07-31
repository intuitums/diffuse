"""What a model actually accepts, asked of LiteLLM rather than assumed.

Diffuse's configuration says *how carefully to review*. Providers say four
mutually incompatible things, and the differences are not cosmetic. Verified
against the pinned ``litellm==1.93.0`` by rendering ``reasoning_effort`` through
its public parameter mapping:

| Route                       | Rendering                                          |
| --------------------------- | -------------------------------------------------- |
| ``anthropic/claude-sonnet-5``   | ``output_config: {effort: high}`` + adaptive thinking |
| ``anthropic/claude-haiku-4-5``  | ``thinking: {type: enabled, budget_tokens: 4096}``    |
| ``gemini/gemini-2.5-pro``       | ``thinkingConfig: {thinkingBudget: 4096}``            |
| ``openai/gpt-5``, ``vllm/…``    | ``reasoning_effort: high`` passed through             |
| ``ollama/…``                    | ``think: true`` — a switch, the grade is discarded    |
| ``openai/gpt-4.1-mini``         | refused outright                                      |

So "review carefully" is a graded effort word on one route, a token budget on
the next, an on/off switch on a third, and unrepresentable on a fourth. Asking
each caller to know which is how the previous design leaked provider syntax into
Diffuse's configuration.

**The resolution is a probe, not a table of model names.** A name table rots on
every model release; the table above is over *mechanisms*, which are few and
change slowly, and even that is only used to read a rendering LiteLLM produced —
nothing here decides what a model supports. Everything is derived from
``get_optional_params``, which runs offline and needs no credential.

Probing the rendering rather than merely asking "did it raise?" is what makes
this more than a rename. Three routes accept the request and then do something
other than what was asked, which a raise/no-raise probe reports as success:

* ``ollama`` renders ``xhigh`` and ``max`` as ``think: false`` — asking for the
  most reasoning turns reasoning off. ``.env.example`` recommends ``xhigh`` and
  lists ``ollama/`` as a route that accepts it.
* ``anthropic/claude-haiku-4-5`` renders ``max`` as a 16384-token thinking
  budget, which is not below the 16000-token default ``max_tokens``; Anthropic
  requires ``budget_tokens < max_tokens``, so that request is a 400.
* ``openrouter/anthropic/claude-sonnet-4.6`` silently clamps ``max`` to
  ``xhigh``, and ``azure/gpt-5`` refuses ``xhigh`` while accepting ``max``.

**And the same premise applies to side effects on a different parameter.**
Asking for reasoning depth can change what a route does with a parameter you did
not touch. On the two ``anthropic/`` routes that reach structured output through
a tool, LiteLLM forces that tool with ``tool_choice`` only when thinking is off
(``AnthropicConfig.map_openai_params`` guards the assignment with
``not is_thinking_enabled``), so a depth request silently makes the JSON tool
optional and the model may answer in prose instead. That is unrepresentable in a
per-parameter probe: it is only visible by rendering the *combination*. See
``plan_structured_output``.

This module is deliberately a leaf, like ``model_providers``: it imports nothing
from ``service``, so ``review_engine`` and ``indexer`` can both depend on it
without a cycle. Keep it that way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType

import litellm

__all__ = [
    "EFFORT_LEVELS",
    "REVIEW_DEPTHS",
    "ModelCapabilities",
    "ReasoningMechanism",
    "ReasoningPlan",
    "StructuredOutputPlan",
    "accepts",
    "depth_for_effort",
    "describe",
    "effort_for_depth",
    "is_known_route",
    "plan_reasoning",
    "plan_structured_output",
    "supports_structured_output",
]

# Diffuse's own vocabulary for review depth, shallow to deep. These name an
# intent -- how carefully to review -- and are never sent anywhere: `plan_reasoning`
# decides what each one becomes on the resolved route.
REVIEW_DEPTHS = ("brisk", "standard", "careful", "thorough", "exhaustive")

# LiteLLM's `reasoning_effort` vocabulary, in the same order, one rung per depth.
# `minimal` and `none` are omitted deliberately: an effort setting that
# suppresses reasoning is better expressed by leaving the depth unset, which
# sends nothing at all.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ReasoningMechanism(StrEnum):
    """How a route expresses reasoning depth, read off LiteLLM's rendering."""

    #: The route has no reasoning control. Nothing can be sent.
    NONE = "none"
    #: A graded effort word reaches the provider (`output_config.effort`, or
    #: `reasoning_effort` passed through).
    EFFORT_SCALE = "effort-scale"
    #: The word is translated into a thinking-token budget
    #: (`thinking.budget_tokens`, `thinkingConfig.thinkingBudget`).
    TOKEN_BUDGET = "token-budget"
    #: The word only turns reasoning on or off; the grade is discarded.
    SWITCH = "switch"


# Where each mechanism shows up in a rendered request. Ordered: a route that
# renders both a graded effort and a thinking block (Anthropic 5 renders
# `output_config.effort` alongside `thinking: {type: adaptive}`) is graded.
_EFFORT_PATHS = (("output_config", "effort"), ("reasoning_effort",))
_BUDGET_PATHS = (
    ("thinking", "budget_tokens"),
    ("thinkingConfig", "thinkingBudget"),
    ("reasoning", "max_tokens"),
)
_SWITCH_KEYS = ("think", "thinking", "reasoning", "enable_thinking", "include_reasoning")
_ENGAGED_THINKING_TYPES = frozenset({"enabled", "adaptive", "auto"})


def effort_for_depth(depth: str) -> str:
    """The LiteLLM effort rung a Diffuse depth sits on."""

    return EFFORT_LEVELS[REVIEW_DEPTHS.index(depth)]


def depth_for_effort(effort: str) -> str:
    """The Diffuse depth an operator-supplied LiteLLM effort rung names."""

    return REVIEW_DEPTHS[EFFORT_LEVELS.index(effort)]


@lru_cache(maxsize=256)
def _route(model: str) -> tuple[str, str] | None:
    """(model, provider) as LiteLLM resolves them, or None if unroutable."""

    try:
        resolved, provider, _, _ = litellm.get_llm_provider(model=model)
    except Exception:
        return None
    return resolved, provider


@lru_cache(maxsize=1024)
def _render(
    model: str,
    parameter: str,
    value: object,
    max_output_tokens: int | None = None,
) -> Mapping[str, object] | None:
    """What LiteLLM would send for ``parameter=value``, or None if refused.

    LiteLLM raises `UnsupportedParamsError` client-side, before any request is
    made, and that is neither an authentication failure nor a `ValueError` -- so
    an unconditional parameter made `worker.run_once` treat a permanent
    misconfiguration as retryable, burning five attempts per review and posting
    a failure notice on every pull request. Omitting a parameter is the safe
    direction, since the model then falls back to its own default, so any probe
    failure declines to send it.

    `max_output_tokens` is threaded through because it changes the answer: the
    budget a route derives from an effort word has to fit inside it.
    """

    route = _route(model)
    if route is None:
        return None
    resolved, provider = route
    arguments: dict[str, object] = {parameter: value}
    if max_output_tokens is not None:
        arguments["max_tokens"] = max_output_tokens
    try:
        rendered = litellm.utils.get_optional_params(
            model=resolved,
            custom_llm_provider=provider,
            **arguments,
        )
    except Exception:
        return None
    if not isinstance(rendered, dict):
        return None
    return MappingProxyType(dict(rendered))


def accepts(
    model: str,
    parameter: str,
    value: object,
    *,
    max_output_tokens: int | None = None,
) -> bool:
    """Whether ``model`` accepts ``parameter=value``, asked of LiteLLM."""

    return _render(model, parameter, value, max_output_tokens) is not None


@lru_cache(maxsize=256)
def is_known_route(model: str) -> bool:
    """Whether LiteLLM holds metadata for this identifier, or only a prefix.

    This is the difference between *"this model has no reasoning control"* and
    *"nothing here knows anything about this model"*, and the two look identical
    once a rendering comes back empty. LiteLLM answers a parameter question for
    an unrecognised identifier from its provider defaults, so an OpenAI-compatible
    self-hosted deployment reached through the conventional ``openai/`` prefix
    renders exactly like ``openai/gpt-4.1-mini``: nothing.

    Measured against the pinned ``litellm==1.93.0``::

        openai/gpt-4.1-mini      known    -> genuinely has no reasoning control
        openai/qwen3-32b         unknown  -> LiteLLM has never heard of it
        hosted_vllm/qwen3-32b    unknown  -> and yet renders effort-scale

    The same server behind ``hosted_vllm/`` proves an unknown identifier says
    nothing about the model behind it, so callers must not treat ``NONE`` on an
    unknown route as a positive finding. ``.env.example`` documents both the
    unprefixed and ``openai/`` spellings for ``REVIEW_API_BASE`` deployments.
    """

    route = _route(model)
    if route is None:
        return False
    try:
        info = litellm.get_model_info(model=route[0], custom_llm_provider=route[1])
    except Exception:
        return False
    return bool(info)


def _at(rendered: Mapping[str, object], path: tuple[str, ...]) -> object | None:
    current: object = rendered
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _switch_state(rendered: Mapping[str, object]) -> bool | None:
    """Whether a boolean/typed reasoning switch is on, or None if absent."""

    for key in _SWITCH_KEYS:
        if key not in rendered:
            continue
        value = rendered[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, Mapping):
            kind = value.get("type")
            if isinstance(kind, str):
                return kind in _ENGAGED_THINKING_TYPES
    return None


@dataclass(frozen=True)
class ReasoningPlan:
    """What one model will actually be sent for one requested depth."""

    model: str
    depth: str
    mechanism: ReasoningMechanism
    #: The `reasoning_effort` value to send, or None when nothing can be sent.
    effort: str | None
    #: The graded effort word or token budget LiteLLM renders from it.
    rendered_effort: str | None
    thinking_tokens: int | None
    #: What LiteLLM would put on the wire. Empty when nothing will be sent.
    rendered: Mapping[str, object]
    #: Whether LiteLLM actually knows this identifier. Only meaningful when the
    #: mechanism is NONE, where it separates "this model has no reasoning
    #: control" from "nothing here knows what this model has". See
    #: `is_known_route`.
    known_route: bool = True
    #: The output budget that refused every thinking budget this route renders,
    #: when that -- rather than the model -- is why nothing can be sent.
    blocking_output_budget: int | None = None

    @property
    def honored(self) -> bool:
        """Whether anything at all will be sent for the requested depth."""

        return self.effort is not None

    @property
    def exact(self) -> bool:
        """Whether the route receives the depth that was actually asked for."""

        if not self.honored or self.mechanism is ReasoningMechanism.SWITCH:
            return False
        if self.effort != effort_for_depth(self.depth):
            return False
        return self.rendered_effort in (None, self.effort)

    def describe(self) -> str:
        """One line an operator can act on, naming what will be sent."""

        requested = f"{self.depth} ({effort_for_depth(self.depth)})"
        if self.mechanism is ReasoningMechanism.NONE:
            if self.blocking_output_budget is not None:
                # The model does have a reasoning control; the output budget is
                # what refuses it. Saying "this model cannot reason" here sends
                # the operator to change REVIEW_MODEL, which fixes nothing.
                return (
                    f"{self.model} expresses {requested} as a thinking budget, and every "
                    "budget it renders is at least as large as "
                    f"REVIEW_MAX_OUTPUT_TOKENS={self.blocking_output_budget} -- which the "
                    "provider rejects -- so NOTHING will be sent. "
                    "REVIEW_MAX_OUTPUT_TOKENS is the binding constraint here, not the model."
                )
            if not self.known_route:
                return (
                    f"LiteLLM has no metadata for {self.model}, so {requested} resolves to "
                    "nothing and NOTHING will be sent. This says nothing about the model "
                    "itself: an OpenAI-compatible deployment reached through an unprefixed "
                    "name or the 'openai/' prefix renders identically to a model that "
                    "genuinely has no reasoning control. If the endpoint does support a "
                    "reasoning control, name it with the prefix of the server that serves "
                    "it (for example 'hosted_vllm/') so LiteLLM can map the parameter."
                )
            return (
                f"{self.model} has no reasoning control on this route: "
                f"{requested} cannot be expressed and NOTHING will be sent. "
                "This model will review at its own default depth."
            )
        if self.mechanism is ReasoningMechanism.TOKEN_BUDGET:
            detail = (
                f"a thinking budget of {self.thinking_tokens} tokens"
                if self.thinking_tokens is not None
                else "a thinking budget"
            )
            sending = f"sending reasoning_effort={self.effort} -> {detail}"
        elif self.mechanism is ReasoningMechanism.SWITCH:
            sending = (
                f"sending reasoning_effort={self.effort}, which this route renders as an "
                "on/off switch: reasoning is enabled but its depth is NOT graded"
            )
        else:
            rendered = self.rendered_effort or self.effort
            sending = f"sending reasoning_effort={self.effort} -> effort {rendered}"
        prefix = "" if self.exact else "NOT AS REQUESTED: "
        return f"{prefix}{self.model} asked for {requested}; {sending}."


def _plan_at(
    model: str, depth: str, effort: str, max_output_tokens: int | None
) -> ReasoningPlan | None:
    """Classify one rung, or None when it does not actually engage reasoning."""

    rendered = _render(model, "reasoning_effort", effort, max_output_tokens)
    if rendered is None:
        return None

    for path in _EFFORT_PATHS:
        value = _at(rendered, path)
        if isinstance(value, str) and value:
            return ReasoningPlan(
                model=model,
                depth=depth,
                mechanism=ReasoningMechanism.EFFORT_SCALE,
                effort=effort,
                rendered_effort=value,
                thinking_tokens=None,
                rendered=rendered,
            )

    for path in _BUDGET_PATHS:
        value = _at(rendered, path)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            continue
        if max_output_tokens is not None and value >= max_output_tokens:
            # Anthropic and Gemini both require the thinking budget to sit
            # strictly inside the output budget. LiteLLM renders the larger
            # number happily and the provider returns 400, so this rung is
            # unusable rather than merely approximate.
            return None
        return ReasoningPlan(
            model=model,
            depth=depth,
            mechanism=ReasoningMechanism.TOKEN_BUDGET,
            effort=effort,
            rendered_effort=None,
            thinking_tokens=value,
            rendered=rendered,
        )

    state = _switch_state(rendered)
    if state is True:
        return ReasoningPlan(
            model=model,
            depth=depth,
            mechanism=ReasoningMechanism.SWITCH,
            effort=effort,
            rendered_effort=None,
            thinking_tokens=None,
            rendered=rendered,
        )
    # Either the switch is explicitly off (ollama renders `xhigh` as
    # `think: false`) or the parameter was accepted and then vanished. Both
    # mean this rung buys nothing; a lower one may still work.
    return None


def _output_budget_blocked(model: str, depth: str, max_output_tokens: int | None) -> bool:
    """Whether a thinking budget was rendered and only ``max_tokens`` refused it.

    `_plan_at` discards such a rung, which is correct -- the request would be a
    400 -- but the discarded reason is the only thing that separates *"choose a
    different model"* from *"raise REVIEW_MAX_OUTPUT_TOKENS"*. At
    ``REVIEW_MAX_OUTPUT_TOKENS=1024`` every rung of ``anthropic/claude-haiku-4-5``
    renders a budget of at least 1024, so the route reports no mechanism at all
    while the model's reasoning control is in perfect working order.
    """

    if max_output_tokens is None:
        return False
    for index in range(REVIEW_DEPTHS.index(depth), -1, -1):
        rendered = _render(model, "reasoning_effort", EFFORT_LEVELS[index], max_output_tokens)
        if rendered is None:
            continue
        for path in _BUDGET_PATHS:
            value = _at(rendered, path)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value >= max_output_tokens:
                return True
    return False


def plan_reasoning(
    model: str,
    depth: str,
    *,
    max_output_tokens: int | None = None,
) -> ReasoningPlan:
    """Resolve a Diffuse depth into what this model will actually be sent.

    Starts at the requested rung and steps *down* until it finds one the route
    both accepts and acts on. Stepping down is not a default -- nothing is
    invented when no depth is requested -- it is the closest honest rendering of
    an explicit request, and `ReasoningPlan.exact` reports whenever the answer
    is an approximation so the caller can say so out loud.
    """

    if depth not in REVIEW_DEPTHS:
        raise ValueError(f"review depth must be one of {', '.join(REVIEW_DEPTHS)}")
    requested = REVIEW_DEPTHS.index(depth)
    for index in range(requested, -1, -1):
        plan = _plan_at(model, depth, EFFORT_LEVELS[index], max_output_tokens)
        if plan is not None:
            return plan
    return ReasoningPlan(
        model=model,
        depth=depth,
        mechanism=ReasoningMechanism.NONE,
        effort=None,
        rendered_effort=None,
        thinking_tokens=None,
        rendered=MappingProxyType({}),
        known_route=is_known_route(model),
        blocking_output_budget=(
            max_output_tokens
            if _output_budget_blocked(model, depth, max_output_tokens)
            else None
        ),
    )


@lru_cache(maxsize=512)
def _render_structured(
    model: str,
    response_model: object,
    effort: str | None,
    forced_tool: str | None,
    max_output_tokens: int | None,
) -> Mapping[str, object] | None:
    """Render a whole structured-output request, not one parameter of it.

    `_render` answers "does the mapper refuse this parameter in isolation?",
    which cannot see a parameter changing what the route does with a *different*
    one. Structured output and reasoning depth interact, so the only honest
    probe renders them together.
    """

    route = _route(model)
    if route is None:
        return None
    resolved, provider = route
    arguments: dict[str, object] = {"response_format": response_model}
    if max_output_tokens is not None:
        arguments["max_tokens"] = max_output_tokens
    if effort is not None:
        arguments["reasoning_effort"] = effort
    if forced_tool is not None:
        arguments["tool_choice"] = {"type": "function", "function": {"name": forced_tool}}
    try:
        rendered = litellm.utils.get_optional_params(
            model=resolved,
            custom_llm_provider=provider,
            **arguments,
        )
    except Exception:
        return None
    if not isinstance(rendered, dict):
        return None
    return MappingProxyType(dict(rendered))


def _forced_tool_name(rendered: Mapping[str, object]) -> str | None:
    """The tool a rendering forces, in either the native or OpenAI shape."""

    choice = rendered.get("tool_choice")
    if not isinstance(choice, Mapping):
        return None
    name = choice.get("name")
    if isinstance(name, str) and name:
        return name
    function = choice.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _rendered_tool_names(rendered: Mapping[str, object]) -> frozenset[str]:
    """Every tool name present in a rendering, in either shape."""

    tools = rendered.get("tools")
    if not isinstance(tools, list):
        return frozenset()
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.add(name)
        function = tool.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


@dataclass(frozen=True)
class StructuredOutputPlan:
    """Whether asking for reasoning depth un-forces structured output.

    Some routes reach structured output by forcing a synthetic JSON tool. On
    those, enabling thinking makes the tool *optional*, so the model may answer
    in prose -- which fails schema validation, is classified as a transient
    fault, and burns every retry the workflow has. Nothing raises: the request
    is valid, it just stopped being constrained.
    """

    model: str
    #: Whether the route forces structured output when no depth is requested.
    forced_without_depth: bool
    #: Whether it is still forced once the depth request is added.
    forced_with_depth: bool
    #: The `tool_choice` to send explicitly to restore forcing, or None when
    #: nothing needs restoring (or nothing can restore it).
    tool_choice: Mapping[str, object] | None

    @property
    def unforced_by_depth(self) -> bool:
        """Whether the depth request is what dropped the constraint."""

        return self.forced_without_depth and not self.forced_with_depth

    @property
    def repaired(self) -> bool:
        """Whether sending `tool_choice` explicitly puts the constraint back."""

        return self.tool_choice is not None


def plan_structured_output(
    model: str,
    response_model: object,
    *,
    depth: str | None,
    max_output_tokens: int | None = None,
) -> StructuredOutputPlan:
    """Resolve whether a depth request loosens this route's structured output.

    Deliberately narrow, because the blanket version is a guaranteed 400 on four
    documented route families. Measured against the pinned ``litellm==1.93.0``:

    * ``anthropic/claude-sonnet-5`` and ``anthropic/claude-haiku-4-5`` force a
      ``json_tool_call`` tool, and lose ``tool_choice`` once depth is requested.
      These are the routes that need repair, and the tool is still in ``tools``,
      so naming it explicitly restores the constraint.
    * ``anthropic/claude-sonnet-4-6`` and ``anthropic/claude-opus-4-6`` use
      Anthropic's native ``output_format`` and render **no tools at all**. Depth
      changes nothing for them -- and adding a ``tool_choice`` would force a tool
      that does not exist.
    * ``openai/``, ``hosted_vllm/`` and ``gemini/`` pass ``response_format``
      through and likewise render no tools. Depth changes nothing.

    So the repair is applied only where the probe shows forcing was present,
    was lost to the depth request, and comes back when asked for by name.
    """

    plain = _render_structured(model, response_model, None, None, max_output_tokens)
    if plain is None:
        return StructuredOutputPlan(model, False, False, None)
    tool = _forced_tool_name(plain)
    if tool is None:
        # Nothing was forced with a tool in the first place, so a depth request
        # has no forcing to remove. Every non-tool route lands here.
        return StructuredOutputPlan(model, False, False, None)
    if depth is None:
        return StructuredOutputPlan(model, True, True, None)

    effort = effort_for_depth(depth)
    with_depth = _render_structured(model, response_model, effort, None, max_output_tokens)
    if with_depth is None or _forced_tool_name(with_depth) is not None:
        # Either the depth request is refused outright -- `plan_reasoning` will
        # not send it either -- or forcing survived it. Nothing to repair.
        return StructuredOutputPlan(model, True, with_depth is not None, None)

    if tool not in _rendered_tool_names(with_depth):
        # The tool itself is gone, not merely unforced. Naming it would force a
        # tool the request does not carry, which is a 400 rather than a fix.
        return StructuredOutputPlan(model, True, False, None)

    repaired = _render_structured(model, response_model, effort, tool, max_output_tokens)
    if repaired is None or _forced_tool_name(repaired) != tool:
        # Asked for the repair and did not get it. Report, do not send.
        return StructuredOutputPlan(model, True, False, None)
    return StructuredOutputPlan(
        model=model,
        forced_without_depth=True,
        forced_with_depth=False,
        tool_choice=MappingProxyType(
            {"type": "function", "function": {"name": tool}}
        ),
    )


@dataclass(frozen=True)
class ModelCapabilities:
    """Everything the probe can say about one model identifier.

    `diffuse model` renders this, and `diffuse init` (W5.2) will show it before
    writing any configuration, so an operator sees what their chosen model
    supports instead of discovering it from a dropped parameter.
    """

    model: str
    routed: bool
    accepts_temperature: bool
    supports_structured_output: bool
    reasoning_mechanism: ReasoningMechanism
    supported_parameters: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "routed": self.routed,
            "accepts_temperature": self.accepts_temperature,
            "supports_structured_output": self.supports_structured_output,
            "reasoning_mechanism": self.reasoning_mechanism.value,
            "supported_parameters": list(self.supported_parameters),
        }


def _reasoning_mechanism(model: str, max_output_tokens: int | None) -> ReasoningMechanism:
    """The deepest mechanism any rung exposes, independent of a request."""

    for index in range(len(EFFORT_LEVELS) - 1, -1, -1):
        plan = _plan_at(model, REVIEW_DEPTHS[index], EFFORT_LEVELS[index], max_output_tokens)
        if plan is not None:
            return plan.mechanism
    return ReasoningMechanism.NONE


@lru_cache(maxsize=256)
def supports_structured_output(model: str) -> bool:
    """Whether the route can be handed a JSON Schema response format."""

    try:
        return bool(litellm.supports_response_schema(model=model))
    except Exception:
        return False


def describe(
    model: str,
    *,
    temperature: float,
    max_output_tokens: int | None = None,
) -> ModelCapabilities:
    """Resolve everything the probe can tell us about ``model``."""

    route = _route(model)
    supported: tuple[str, ...] = ()
    if route is not None:
        try:
            names = litellm.utils.get_supported_openai_params(
                model=route[0], custom_llm_provider=route[1]
            )
        except Exception:
            names = None
        supported = tuple(sorted(names or ()))
    return ModelCapabilities(
        model=model,
        routed=route is not None,
        accepts_temperature=accepts(model, "temperature", temperature),
        supports_structured_output=supports_structured_output(model),
        reasoning_mechanism=_reasoning_mechanism(model, max_output_tokens),
        supported_parameters=supported,
    )
