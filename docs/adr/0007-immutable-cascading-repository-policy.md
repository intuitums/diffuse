# ADR 0007: Immutable cascading repository policy

- Status: accepted
- Date: 2026-07-23

## Context

Review customization affects what source is retrieved, which model passes run,
which findings survive verification, and what is published. Loading mutable
rules at generation time would make a review impossible to reproduce and could
reuse an idempotent result generated under different policy. Passing ignored
paths or confidence preferences only through a prompt would also fail to
enforce them deterministically.

Repository instruction and context files are attacker-controlled input on
untrusted pull requests. Discovery must not follow symlinks, read untracked
host files, traverse parent directories, or allow unbounded prompt content.

## Decision

Diffuse treats repository policy as part of an immutable index snapshot.

- Strict, versioned `.diffuse/config.json` files may appear at any directory.
  Settings resolve root-to-leaf for each changed path. Nested scalar values
  replace inherited values and ignored-path matches accumulate.
- Structured rules use stable IDs, bounded review guidance, path scopes,
  severity/category metadata, and explicit nested overrides.
- `.diffuse/rules.md` supplies scoped Markdown guidance.
- `.diffuse/files.json` references tracked context files with path scopes and
  optional descriptions.
- Common `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.cursorrules`, Cursor
  MDC, and GitHub Copilot instruction files are discovered automatically.
- Discovery uses Git's tracked-file list and accepts regular UTF-8 files only.
  Paths, globs, counts, per-file sizes, aggregate size, and prompt size are
  bounded. Invalid or ambiguous policy fails the index rather than being
  ignored.
- Policy layers and guidance documents are stored with content hashes and a
  canonical policy fingerprint. Snapshot validation counts them before atomic
  activation. The index-format identifier includes the policy schema version.
- Workers select one compatible snapshot before policy resolution. Disabled or
  ignored diff files are removed before lexical, graph, or semantic retrieval.
- Pass selection and path confidence floors are enforced in code. Summary-only
  mode suppresses inline comments. A fully disabled review is durably marked
  `skipped` and produces no SCM publication.
- The review context fingerprint combines the selected snapshot ID with the
  effective per-path policy and deployment defaults. It participates in the
  review-run uniqueness contract.
- Repository guidance is presented in a dedicated bounded block. It can refine
  review criteria but cannot override system safety, exact-diff grounding, or
  structured-output constraints.

## Consequences

Policy changes are reviewed and versioned alongside code, indexing failures are
visible instead of silently weakening controls, and every review records the
exact policy/index context that produced it. Ignore, confidence, and
publication controls do not depend on model obedience.

The current foundation is repository-scoped. Organization/team/dashboard rules,
authorized API mutation, audit history, richer Cursor front matter, negative
glob patterns, instruction precedence interoperability, and generated
summaries/embeddings for policy documents remain parity work. ADR 0034 adds a
strict compatibility import for current public root `greptile.json` concepts.
