# ADR 0017: Deterministic review confidence and publication identity

Date: 2026-07-23

Status: Accepted

## Context

A published review needs to expose a 0–5 confidence score, a review counter, a
link to the last reviewed commit, and a re-trigger control. Diffuse already
stored a 0–10 risk score and pinned GitHub review publication to a commit, but
it did not expose a distinct merge-readiness score or a durable human-readable
review sequence.

Letting a model state its own confidence would make the merge signal hard to
audit. Counting remote comments at render time would make numbering sensitive
to eventual consistency, deleted comments, and publication retries.

## Decision

- Derive a 0–5 confidence score in trusted code after independent finding
  verification. Begin with the verified 0–10 risk band and conservatively cap
  the result for finding volume, incomplete coverage, ignored files, or zero
  reviewed files.
- Persist confidence separately from risk on every review report. Publish both
  values in the GitHub review and optional status check.
- Require exactly 5/5 as one of the automatic-approval gates; zero model
  findings alone is insufficient.
- Assign a positive per-pull-request review number when publication begins.
  Serialize assignment by locking the pull-request row, enforce uniqueness in
  PostgreSQL, and reuse the stored number across retries.
- Render the exact commit SHA as an SCM link and expose the already-supported
  `@diffuse review` manual trigger in the footer.

## Consequences

Review readiness is explainable from durable evidence and cannot be inflated
by model prose. Operators and developers can identify review order and exact
commit scope even after retries, while the visible re-trigger instruction maps
to an existing authorized webhook path.

This does not yet provide auto-selected diagrams, configurable/collapsible
sections, commit-message previews, a web history view, or GitLab publication.
