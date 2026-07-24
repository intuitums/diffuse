# ADR 0034: Strict `greptile.json` compatibility import

Date: 2026-07-23

Status: Accepted

## Context

Diffuse's immutable cascading `.diffuse/` policy is more expressive than the
legacy root `greptile.json` format, but requiring a repository migration before
its first self-hosted review creates unnecessary adoption risk. Greptile's
current public contract still supports `greptile.json` at the repository root
and gives its native folder format higher precedence.

A compatibility layer cannot silently discard settings. Trigger, path, and
publication differences can make a repository unexpectedly review more code,
publish more output, or weaken a configured signal threshold.

Relevant public contract:

- <https://www.greptile.com/docs/code-review/greptile-json-reference>

## Decision

- Discover a tracked root `greptile.json` only when the repository has no
  tracked root `.diffuse/config.json`, `.diffuse/rules.md`, or
  `.diffuse/files.json`. Native root policy causes the compatibility file to be
  ignored as a whole; nested native layers may still refine an imported root.
- Parse a strict bounded schema with duplicate-key protection and reject
  unknown fields.
- Translate automatic/draft/update triggers, PR filters, newline keywords,
  file-change limits, status checks, summary-only mode, footer and output
  sections to native policy fields.
- Translate root Git-style ignore names and directory patterns into Diffuse
  globs. Reject negation, escapes, and character classes until their semantics
  are implemented rather than approximating them.
- Import cross-repository context and pattern repositories under the same
  seven-repository bound.
- Import free-form instructions, scoped custom rules, scoped inline context,
  and tracked scoped context files as content-hashed snapshot guidance.
  Referenced files retain the existing regular-file, UTF-8, size, traversal,
  and tracked-source checks.
- Map `strictness` levels `1`, `2`, and `3` to `low`, `medium`, and `high`
  minimum finding severity. Enforce the threshold in code after the independent
  verifier chooses the final severity.
- Preserve `commentTypes` as bounded repository guidance with concrete category
  definitions; it cannot suppress directly evidenced security defects.
- Map `shouldUpdateDescription`, `fixWithAI`, and `statusCommentsEnabled` to
  immutable native description-target, fix-guidance, and summary-comment
  controls. Provider publication preserves those outcomes on GitHub and
  GitLab.

## Consequences

Repositories can move their current safe review controls and context into a
self-hosted Diffuse installation without a flag-day rewrite. The imported
layer, generated rules, context hashes, severity floor, and tracked source
identity participate in the normal immutable policy fingerprint and review
provenance.

Some valid Gitignore patterns still block indexing until converted or
implemented. Dashboard settings are not represented by a repository file and
therefore are not imported. Native `.diffuse/` remains the path for cascading
configuration, stable custom rule IDs, override controls,
preventative-security policy, and automatic approval.
