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


def test_release_artifacts_ship_the_license():
    """Both public distribution formats must carry the BSL text."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text()
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert "cp LICENSE deploy/README.md" in workflow
    assert "COPY --chown=diffuse:diffuse LICENSE /opt/diffuse/LICENSE" in dockerfile
    assert "test -f /opt/diffuse/LICENSE" in dockerfile


def test_release_requires_public_oci_artifacts():
    """A release must prove operators can pull both artifacts anonymously."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "Verify anonymous release access" in workflow
    assert "oras logout ghcr.io" in workflow
    assert 'oras manifest fetch "${IMAGE}@${DIGEST}"' in workflow
    assert 'oras manifest fetch "${BUNDLE_ARTIFACT}:${RELEASE_TAG}"' in workflow


def _declared_values(path: Path) -> dict[str, str]:
    """Every assignment, whether it is live or a commented recommendation.

    `REVIEW_MODEL` ships commented out in both files so that `cp .env.example
    .env` cannot produce a working review model -- that would be the deleted
    code default moved into a file. The recommendation itself still has to stay
    identical between the two, which is the drift a customer feels, so the
    comparison below has to see a commented line as well as a live one.
    """

    return dict(
        re.findall(r"^#?([A-Z][A-Z0-9_]*)=(.*)$", path.read_text(), re.M)
    )


def _live_assignments(path: Path) -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", path.read_text(), re.M))


def test_review_model_is_not_set_by_copying_an_example_file():
    """Both env files must recommend a review model without supplying one.

    deploy/README.md tells a customer to copy `env.example` to `.env`, and
    `.env.example` is copied the same way in development. While `REVIEW_MODEL`
    was assigned there, that copy produced a working Anthropic configuration --
    which is exactly the guess `review_model()` stopped making, one file over.
    """

    for path in (SOURCE_ENV, CUSTOMER_ENV):
        assert "REVIEW_MODEL" not in _live_assignments(path), (
            f"{path.name} assigns REVIEW_MODEL. Comment the line out: copying "
            "this file must not hand an operator a model they never chose."
        )
        assert "#REVIEW_MODEL=" in path.read_text(), (
            f"{path.name} no longer carries the commented REVIEW_MODEL "
            "recommendation, so an operator has nothing to uncomment."
        )


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


def test_no_code_default_can_drift_from_the_shipped_env_files():
    """The drift this used to check for is now impossible by construction.

    This test used to assert `deploy/env.example` matched a
    `DEFAULT_REVIEW_MODEL` constant in the code. That constant is gone:
    guessing a provider the operator never named was the defect, not the
    particular model guessed. `REVIEW_MODEL` in the env files is a
    recommendation an operator edits, not a fallback anything reads, so the two
    can no longer disagree. The sibling test above still keeps the two env
    files themselves in sync, which is the drift a customer can actually feel.
    """
    from service import review_engine

    assert not hasattr(review_engine, "DEFAULT_REVIEW_MODEL")
