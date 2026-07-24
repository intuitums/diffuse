# ADR 0005: Versioned multi-language code graph adapters

- Status: accepted
- Date: 2026-07-23

## Context

Semantic retrieval alone cannot reliably identify affected callers, contracts,
implementations, or tests. The first graph extractor supported only Python,
while regex-based chunk boundaries for other languages silently produced no
symbols or relationships. Parser upgrades could also change graph output for
the same commit without changing the snapshot identity.

A self-hosted and air-gapped worker must not download executable parser
artifacts when it first encounters a language.

## Decision

Diffuse uses one provider-neutral graph contract with versioned language
adapters.

- Python continues to use the standard-library AST for precise import alias,
  relative import, call, definition, variable, and inheritance handling.
- JavaScript/JSX, TypeScript/TSX, Go, Java, Ruby, Rust, PHP, C, and C++ use
  locally installed per-language Tree-sitter wheels.
- No grammar is fetched at indexing time. Docker builds install the same parser
  dependencies as the application.
- Adapters emit modules, namespaces, classes, interfaces, traits, enums,
  implementations, functions, methods, variables, and type aliases where the
  language grammar exposes them.
- Relationships currently cover containment, imports, calls, inheritance, and
  implementation. Repository linking first uses exact qualified names, then
  conservative unique same-language suffix/name matches.
- Stable symbol keys depend on file, kind, qualified name, and deterministic
  occurrence, not body content. Body hashes still change when implementation
  text changes.
- Parser-backed definitions drive chunk boundaries so graph citations and
  retrieved chunks use the same line model.
- Files are bounded before parsing. Invalid syntax creates a diagnostic for the
  affected file while valid files continue indexing.
- Snapshot `index_format_version` contains a fingerprint of the adapter schema,
  Python runtime, Tree-sitter runtime, and installed grammar versions.
  Retrieval will not use an active snapshot built by a different format.

## Consequences

Cross-file impact retrieval now works across the primary roadmap languages,
and parser changes trigger explicit re-indexing rather than silently changing
the meaning of an existing snapshot. Individual grammar wheels increase image
size but avoid a broad runtime language pack and runtime executable downloads.

The foundation does not yet provide full compiler-grade type resolution,
conditional-build interpretation, macro expansion, generated-code mapping, or
stable rename identity. Language-specific eval sets and richer usage,
interface, test, schema, and package-resolution edges remain required for full
parity.
