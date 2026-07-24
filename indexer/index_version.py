"""Version identifiers for immutable index compatibility."""

from __future__ import annotations

import hashlib
import platform
from importlib.metadata import PackageNotFoundError, version

from repository_policy.models import POLICY_SCHEMA_VERSION

LANGUAGE_ADAPTER_SCHEMA_VERSION = "language-adapters-v1"
PARSER_DISTRIBUTIONS = (
    "tree-sitter",
    "tree-sitter-c",
    "tree-sitter-cpp",
    "tree-sitter-go",
    "tree-sitter-java",
    "tree-sitter-javascript",
    "tree-sitter-php",
    "tree-sitter-ruby",
    "tree-sitter-rust",
    "tree-sitter-typescript",
)


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "missing"


_PARSER_RUNTIME_IDENTITY = "|".join(
    (
        LANGUAGE_ADAPTER_SCHEMA_VERSION,
        POLICY_SCHEMA_VERSION,
        f"python-{platform.python_version()}",
        *(f"{name}-{_distribution_version(name)}" for name in PARSER_DISTRIBUTIONS),
    )
)
PARSER_RUNTIME_FINGERPRINT = hashlib.sha256(
    _PARSER_RUNTIME_IDENTITY.encode()
).hexdigest()[:16]
INDEX_FORMAT_VERSION = (
    f"diffuse-index-v3-policy-{PARSER_RUNTIME_FINGERPRINT}"
)
