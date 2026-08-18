"""Version identifiers for immutable index compatibility."""

from __future__ import annotations

import hashlib
import platform
from importlib.metadata import PackageNotFoundError, version

from diffuse.repository.policy.models import POLICY_SCHEMA_VERSION

LANGUAGE_ADAPTER_SCHEMA_VERSION = "language-adapters-v2-typescript-extends"
# Grammar upgrades change the symbols and relationships extracted from the same
# source, so their versions are part of the index compatibility fingerprint.
#
# `_distribution_version` reads these at runtime through `importlib.metadata`,
# which needs the distribution metadata to be present. PyInstaller only bundles
# metadata it can see statically, and it cannot see a `version(name)` call whose
# argument is a loop variable — so every entry here needs a matching
# `--copy-metadata` flag in the Dockerfile, which asserts their presence at
# build time. Without that, each lookup returns "missing", the fingerprint
# freezes, and an upgraded image silently reuses indexes built by a different
# grammar.
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
# v5 adds an immutable whole-file corpus for literal, line-oriented grep.  A
# v4 snapshot has no such corpus, so it must be rebuilt instead of silently
# returning incomplete grep results.
#
# v4 dropped the embedding column: a v3 snapshot's chunks are still readable,
# but snapshot compatibility no longer carries an embedding model or dimension,
# so a v3 row cannot be distinguished from one built by a different embedder.
# Bumping forces a rebuild rather than trusting a snapshot whose provenance the
# schema no longer records.
INDEX_FORMAT_VERSION = (
    f"diffuse-index-v5-graph-lexical-grep-{PARSER_RUNTIME_FINGERPRINT}"
)
