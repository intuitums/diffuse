"""`deploy/env.example` must stay a complete reference for customers.

The release bundle ships only `deploy/README.md`, `deploy/compose.yaml`, and
`deploy/env.example` (see `.github/workflows/release.yml`). The source
`.env.example` is *not* in the bundle, so anything documented only there is
invisible to a self-hosted customer.

That gap had already grown to 50 variables, including security-relevant ones a
customer needs to reason about: `DIFFUSE_ALLOW_PLAINTEXT_ORIGINS`,
`GITLAB_WEBHOOK_MAX_AGE_SECONDS`, the `GITHUB_OAUTH_*` settings, and
`MIN_REVIEW_CONFIDENCE`. These tests keep the two files in sync so it cannot
silently reopen.
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

# Only meaningful in the customer bundle: digest pinning and secret placement.
CUSTOMER_ONLY = frozenset(
    {
        "DIFFUSE_IMAGE",
        "POSTGRES_IMAGE",
        "DIFFUSE_ENV_FILE",
        "DIFFUSE_SECRETS_DIR",
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
        "GITLAB_WEBHOOK_MAX_AGE_SECONDS",
        "GITHUB_ALLOWED_INSTANCES",
        "GITLAB_ALLOWED_INSTANCES",
        "MIN_REVIEW_CONFIDENCE",
        "SCM_API_TIMEOUT_SECONDS",
    ):
        assert name in declared, f"{name} is not documented for customers"


def test_release_bundle_ships_the_customer_env_example():
    """If the bundle stops shipping this file, the tests above stop meaning anything."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "deploy/env.example" in workflow
