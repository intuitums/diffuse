"""Named environment contracts shared by local agent hosting and its compartment."""

from __future__ import annotations

# The credential directory is intentionally explicit so container preflight can
# require the same path the agent host will use, rather than trusting a default
# selected from an ambient HOME.
AGENT_HOME_VARIABLE = "DIFFUSE_AGENT_HOME"

# Named here only so tests and the review-compartment preflight can assert their
# absence individually. The child allowlist remains the stronger control: it
# also excludes credentials nobody has remembered to add to this tuple.
CREDENTIAL_ENVIRONMENT = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "SSH_AUTH_SOCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "DIFFUSE_GIT_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DIFFUSE_API_TOKEN",
    "POSTGRES_PASSWORD",
)
