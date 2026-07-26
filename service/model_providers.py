"""Single source of truth for LiteLLM model-identifier resolution.

Three call sites need to answer questions about the same model string: which
credential it needs (``review_engine._model_api_key``), whether an
operator-configured ``REVIEW_API_BASE`` applies to it
(``review_engine._model_api_base``), which provider to report in readiness
output (``model_cli``), and which family it belongs to for provenance routing
(``review_provenance.model_family``).

Those answers previously lived in three independently maintained prefix tables
that had already drifted: identifiers such as ``vertex_ai/…``, ``azure_ai/…``,
and bare ``gemini-…`` fell through to a "custom" branch that declared no
credential requirement, so ``diffuse model`` reported a deployment ready when
no credential was set at all.

This module is deliberately a leaf: it imports nothing from ``service`` so that
``review_provenance`` and ``review_engine`` can both depend on it without a
cycle.

Provider and family are separate resolutions because they genuinely differ. A
gateway route such as ``openrouter/anthropic/claude-sonnet-4.6`` needs the
OpenRouter credential but belongs to the Anthropic family, and
``bedrock/anthropic.claude-…`` needs AWS credentials while also belonging to the
Anthropic family.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRecord:
    """How to authenticate a model identifier and whether it takes a base URL."""

    provider_id: str
    credential_env_names: tuple[str, ...]
    credential_required: bool
    accepts_custom_api_base: bool
    credential_value_is_api_key: bool = True
    ambient_credentials_supported: bool = False


_OPENAI_PREFIXES = ("openai/", "gpt-", "o1", "o3", "o4")
_ANTHROPIC_PREFIXES = ("anthropic/", "claude")
_GOOGLE_PREFIXES = ("gemini/", "google/", "gemini")
_VERTEX_AI_PREFIXES = ("vertex_ai/",)
_SELF_HOSTED_PREFIXES = ("ollama/", "hosted_vllm/")

# Ordered: the first matching prefix wins. ``accepts_custom_api_base`` is False
# for every provider that reaches its own managed endpoint — pointing those at
# an operator's OpenAI-compatible server would send that provider's model name
# and credential to the wrong host.
_PROVIDER_TABLE: tuple[tuple[tuple[str, ...], ProviderRecord], ...] = (
    (
        ("openrouter/",),
        ProviderRecord("openrouter", ("OPENROUTER_API_KEY",), True, False),
    ),
    (
        _ANTHROPIC_PREFIXES,
        ProviderRecord("anthropic", ("ANTHROPIC_API_KEY",), True, False),
    ),
    (
        ("bedrock/",),
        ProviderRecord(
            "aws-bedrock",
            ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"),
            True,
            False,
            credential_value_is_api_key=False,
            ambient_credentials_supported=True,
        ),
    ),
    (
        ("azure/", "azure_ai/"),
        ProviderRecord("azure", ("AZURE_API_KEY",), True, False),
    ),
    (
        _OPENAI_PREFIXES,
        ProviderRecord("openai", ("OPENAI_API_KEY", "OPENAI_KEY"), True, True),
    ),
    (
        _VERTEX_AI_PREFIXES,
        ProviderRecord(
            "google",
            (
                "GOOGLE_APPLICATION_CREDENTIALS",
                "VERTEXAI_PROJECT",
                "VERTEXAI_LOCATION",
            ),
            True,
            False,
            credential_value_is_api_key=False,
            ambient_credentials_supported=True,
        ),
    ),
    (
        _GOOGLE_PREFIXES,
        ProviderRecord(
            "google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), True, False
        ),
    ),
    (
        _SELF_HOSTED_PREFIXES,
        ProviderRecord("self-hosted", ("REVIEW_API_BASE",), False, True),
    ),
)

_CUSTOM_PROVIDER = ProviderRecord("custom", ("REVIEW_API_BASE",), False, True)


def resolve_provider(model: str) -> ProviderRecord:
    """Resolve the credential and base-URL contract for a model identifier."""

    normalized = model.strip().casefold()
    for prefixes, record in _PROVIDER_TABLE:
        if normalized.startswith(prefixes):
            return record
    return _CUSTOM_PROVIDER


def model_family(model: str) -> str | None:
    """Infer a provider family from a LiteLLM/OpenRouter model identifier.

    Gateway prefixes are stripped first so that a routed identifier resolves to
    the family of the model actually being served, which is what provenance
    routing needs in order to pick an opposing family.
    """

    normalized = model.strip().casefold()
    if normalized.startswith("openrouter/"):
        normalized = normalized.removeprefix("openrouter/")
    if (
        normalized.startswith((*_ANTHROPIC_PREFIXES, "bedrock/anthropic"))
        or "/anthropic/" in normalized
    ):
        return "anthropic"
    if (
        normalized.startswith((*_OPENAI_PREFIXES, "azure/", "azure_ai/"))
        or "/openai/" in normalized
    ):
        return "openai"
    if (
        normalized.startswith((*_GOOGLE_PREFIXES, *_VERTEX_AI_PREFIXES))
        or "/google/" in normalized
    ):
        return "google"
    return None
