# ADR 0004: Native structured review generation and publication

Date: 2026-07-23

Status: Accepted

## Context

An external review CLI that generates and publishes in one opaque subprocess
cannot give Diffuse durable findings, stable evidence, provider-independent
review state, exact index provenance, or reliable recovery after a partial SCM
write. Free-form model output also cannot safely become an external comment:
repository content may attempt prompt injection, locations may not exist in the
diff, and malformed output can create noisy or misleading reviews.

## Decision

Diffuse owns a versioned native review pipeline.

- A unified-diff parser records exact added `RIGHT` and deleted `LEFT` lines.
- Bounded specialized passes cover correctness, security, performance, and
  tests/contracts.
- Diffs and retrieved repository context are labeled as untrusted data.
- Model responses must satisfy strict Pydantic schemas. Provider-native
  response schemas are used when supported; prompt-schema fallback is still
  validated locally.
- Candidates that do not cite an exact changed line are discarded
  deterministically before a separate conservative verifier sees them.
- Candidates are deduplicated and filtered by a configurable confidence floor.
- Critical and high-severity findings impose minimum risk scores.
- Review runs persist the exact base/head SHAs, immutable index snapshot,
  model, prompt version, report, findings, coverage, and token counts.
- Generation and SCM publication are separate durable states. Newer PR heads
  can supersede work before publication.
- GitHub reviews are pinned to the reviewed head commit and use line/side
  locations. A hidden run marker recovers the crash window between GitHub
  accepting a review and PostgreSQL recording its external ID.
- A rejected inline payload falls back to a summary review containing the
  validated findings rather than dropping the review.
- PR-Agent is no longer a runtime dependency.

## Consequences

Diffuse can inspect, query, evaluate, and later converse over its own findings.
Retries reuse successfully generated reports, and malformed model output never
reaches GitHub.

The first implementation remains intentionally bounded. It does not yet
publish GitLab reviews or checks, execute runtime validation, apply learned
team preferences, or guarantee provider-level exactly-once behavior beyond
marker recovery. Cascading repository policy, durable GitHub status checks,
finding-thread continuity, grounded GitHub thread conversation, and
inspectable feedback capture are defined by ADRs 0007 through 0012.
