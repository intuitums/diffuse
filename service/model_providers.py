"""Single source of truth for LiteLLM model-identifier resolution.

Three call sites need to answer questions about the same model string: which
credential it needs (``review.engine._model_api_key``), whether an
operator-configured ``REVIEW_API_BASE`` applies to it
(``review.engine._model_api_base``), which provider to report in readiness
output (``cli.model``), and which family it belongs to for provenance routing
(``review.provenance.model_family``).

Those answers previously lived in three independently maintained prefix tables
that had already drifted: identifiers such as ``vertex_ai/…``, ``azure_ai/…``,
and bare ``gemini-…`` fell through to a "custom" branch that declared no
credential requirement, so ``diffuse model`` reported a deployment ready when
no credential was set at all.

This module is deliberately a leaf: it imports nothing from ``service`` so that
``review.provenance`` and ``review.engine`` can both depend on it without a
cycle.

Provider and family are separate resolutions because they genuinely differ. A
gateway route such as ``openrouter/anthropic/claude-sonnet-4.6`` needs the
OpenRouter credential but belongs to the Anthropic family, and
``bedrock/anthropic.claude-…`` needs AWS credentials while also belonging to the
Anthropic family.
"""

from __future__ import annotations

import re
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
# LiteLLM route prefixes that name an endpoint the operator runs. These are the
# only prefixed identifiers allowed to reach ``REVIEW_API_BASE`` without a
# credential; a prefix absent from this module is treated as a managed provider
# Diffuse has not been taught about, not as a local deployment.
_SELF_HOSTED_PREFIXES = (
    "ollama/",
    "ollama_chat/",
    "hosted_vllm/",
    "vllm/",
    "lm_studio/",
    "litellm_proxy/",
    "openai_like/",
    "custom_openai/",
)

# A LiteLLM provider prefix, used to name the credential an unlisted managed
# provider conventionally reads (``mistral/…`` → ``MISTRAL_API_KEY``).
_PROVIDER_PREFIX_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,38}[a-z0-9])?")

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
_UNKNOWN_PROVIDER = ProviderRecord("unknown", (), True, False)


def _unlisted_managed_provider(normalized: str) -> ProviderRecord:
    """Contract for a ``provider/model`` route this table does not enumerate.

    LiteLLM supports far more providers than Diffuse names above. Treating
    ``mistral/…``, ``groq/…``, or ``xai/…`` as a custom local deployment made
    ``diffuse model`` report ``credential_configured`` for a provider whose key
    was never set, and — when ``REVIEW_API_BASE`` was configured for the other,
    genuinely self-hosted model of the pair — sent that provider's model name to
    the operator's own server. An unrecognised prefix is a managed provider:
    require its conventional key and never apply the local base URL. Operators
    pointing Diffuse at their own endpoint declare it through
    ``_SELF_HOSTED_PREFIXES`` or an unprefixed deployment name.
    """

    prefix = normalized.partition("/")[0]
    if not _PROVIDER_PREFIX_PATTERN.fullmatch(prefix):
        return _UNKNOWN_PROVIDER
    return ProviderRecord(
        prefix,
        (f"{prefix.upper().replace('-', '_')}_API_KEY",),
        True,
        False,
    )


def resolve_provider(model: str) -> ProviderRecord:
    """Resolve the credential and base-URL contract for a model identifier."""

    normalized = model.strip().casefold()
    for prefixes, record in _PROVIDER_TABLE:
        if normalized.startswith(prefixes):
            return record
    if "/" in normalized:
        return _unlisted_managed_provider(normalized)
    # An unprefixed identifier is a deployment name on the operator's own
    # OpenAI-compatible endpoint, which is what ``REVIEW_API_BASE`` exists for.
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
