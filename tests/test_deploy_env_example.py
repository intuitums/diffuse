"""`deploy/env.example` must stay a complete reference for customers.

The release bundle ships only `deploy/README.md`, `deploy/compose.yaml`, and
`deploy/env.example` (see `.github/workflows/release.yml`). The source
`.env.example` is *not* in the bundle, so anything documented only there is
invisible to a self-hosted customer.

That gap had already grown to 50 variables, including security-relevant ones a
customer needs to reason about: `DIFFUSE_ALLOW_PLAINTEXT_ORIGINS`, the
`GITHUB_OAUTH_*` settings, and `MIN_REVIEW_CONFIDENCE`. These tests keep the
two files in sync so it cannot silently reopen.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ENV = REPOSITORY_ROOT / ".env.example"
CUSTOMER_ENV = REPOSITORY_ROOT / "deploy" / "env.example"

# Compose sets these itself, or they only mean something in a source workspace.
# A customer who set them by hand would be overriding the bundle's own wiring.
COMPOSE_SUPPLIED = frozenset(
    {
        "DATABASE_URL",
        "DIFFUSE_SQL_DIR",
        "DIFFUSE_GIT_ASKPASS",
    }
)

# Only meaningful in the customer bundle: digest pinning and env-file placement.
#
# DIFFUSE_SECRETS_DIR used to be here. It configured one thing -- a read-only bind
# mount for the browser sign-in client secret -- and that mount is gone from both
# Compose files because nothing reads a secret from it: there is no `diffuse login`
# subcommand, so the flow cannot be completed. Put both back together, or not at all.
CUSTOMER_ONLY = frozenset(
    {
        "DIFFUSE_IMAGE",
        "POSTGRES_IMAGE",
        "DIFFUSE_ENV_FILE",
    }
)


def _declared(path: Path) -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", path.read_text(), re.M))


def test_customer_env_example_documents_every_source_variable():
    missing = _declared(SOURCE_ENV) - _declared(CUSTOMER_ENV) - COMPOSE_SUPPLIED
    assert missing == set(), (
        "These variables are documented in .env.example but absent from "
        "deploy/env.example, which is the only env file a customer receives: "
        f"{sorted(missing)}. Add them there, or add them to COMPOSE_SUPPLIED if "
        "Compose really does supply them."
    )


def test_customer_env_example_declares_nothing_unknown():
    """A variable only in the customer file is either intentional or a typo."""
    unexpected = _declared(CUSTOMER_ENV) - _declared(SOURCE_ENV) - CUSTOMER_ONLY
    assert unexpected == set(), (
        f"deploy/env.example declares variables the source file does not: "
        f"{sorted(unexpected)}. Either document them in .env.example too, or add "
        "them to CUSTOMER_ONLY."
    )


def test_security_relevant_settings_reach_customers():
    """Named explicitly, because these are the ones whose absence is dangerous.

    An operator cannot make an informed decision about a setting they cannot see.
    """
    declared = _declared(CUSTOMER_ENV)
    for name in (
        "DIFFUSE_ALLOW_PLAINTEXT_ORIGINS",
        "GITHUB_ALLOWED_INSTANCES",
        "MIN_REVIEW_CONFIDENCE",
        "SCM_API_TIMEOUT_SECONDS",
    ):
        assert name in declared, f"{name} is not documented for customers"


def test_release_bundle_ships_the_customer_env_example():
    """If the bundle stops shipping this file, the tests above stop meaning anything."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "deploy/env.example" in workflow


def test_release_requires_public_oci_artifacts():
    """A release must prove operators can pull both artifacts anonymously."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "Verify anonymous release access" in workflow
    assert "oras logout ghcr.io" in workflow
    assert 'oras manifest fetch "${IMAGE}@${DIGEST}"' in workflow
    assert 'oras manifest fetch "${BUNDLE_ARTIFACT}:${RELEASE_TAG}"' in workflow


def _declared_values(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", path.read_text(), re.M))


# Names being in sync is not enough: the customer bundle shipped
# REVIEW_MODEL=openai/gpt-4.1-mini while the code default and `.env.example` both
# said anthropic/claude-sonnet-5. `deploy/README.md` tells the customer to copy
# this file to `.env`, so the documented production install silently ran the
# budget model the source tree calls "the previous default". The tests above all
# passed throughout, because none of them ever compared a value.
MODEL_SETTINGS = ("REVIEW_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS")


def test_customer_env_example_ships_the_same_model_defaults():
    source = _declared_values(SOURCE_ENV)
    customer = _declared_values(CUSTOMER_ENV)
    for name in MODEL_SETTINGS:
        assert customer[name] == source[name], (
            f"{name} differs between the two env files: "
            f".env.example={source[name]!r} deploy/env.example={customer[name]!r}. "
            "A customer following deploy/README.md would get the second one."
        )


def test_shipped_review_model_matches_the_code_default():
    """The env files and the code must not drift apart either."""
    from service.review_engine import DEFAULT_REVIEW_MODEL

    assert _declared_values(CUSTOMER_ENV)["REVIEW_MODEL"] == DEFAULT_REVIEW_MODEL
