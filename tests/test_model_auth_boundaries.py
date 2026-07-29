"""Model credentials must stay on explicit API/CLI executor boundaries."""

from __future__ import annotations

import re
from pathlib import Path

from service.model_providers import resolve_provider
from tests.test_env_documentation import environment_variables_read

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# These are vendor account/session credentials, not Diffuse configuration.
# Future CLI executors must invoke the vendor CLI and let it own its credential
# store; Diffuse must not grow an environment-variable path for these values.
MODEL_ACCOUNT_TOKEN_VARIABLES = frozenset(
    {
        "CHATGPT_ACCESS_TOKEN",
        "CHATGPT_REFRESH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "CODEX_OAUTH_TOKEN",
        "OPENAI_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)


def _declared_environment_variables(path: Path) -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", path.read_text(), re.M))


def test_diffuse_does_not_read_model_account_tokens() -> None:
    assert MODEL_ACCOUNT_TOKEN_VARIABLES.isdisjoint(environment_variables_read())


def test_shipped_environment_does_not_request_model_account_tokens() -> None:
    for relative in (".env.example", "deploy/env.example"):
        declared = _declared_environment_variables(REPOSITORY_ROOT / relative)
        assert MODEL_ACCOUNT_TOKEN_VARIABLES.isdisjoint(declared)


def test_openai_and_anthropic_api_key_paths_remain_explicit() -> None:
    openai = resolve_provider("openai/gpt-5.4")
    anthropic = resolve_provider("anthropic/claude-sonnet-5")

    assert openai.credential_env_names == ("OPENAI_API_KEY", "OPENAI_KEY")
    assert openai.credential_value_is_api_key is True
    assert anthropic.credential_env_names == ("ANTHROPIC_API_KEY",)
    assert anthropic.credential_value_is_api_key is True
