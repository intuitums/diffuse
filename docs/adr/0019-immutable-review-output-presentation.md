# ADR 0019: Immutable review output presentation

Date: 2026-07-23

Status: Accepted

## Context

Repository configuration needs to expose inclusion, collapse, and default-open
behavior for summary, issues-table, confidence-score, and diagram sections, plus
a footer visibility switch.

Diffuse publication is retryable and may happen after repository configuration
changes. Re-resolving presentation at publication time would make identical
review runs render differently. Also, treating an issues-table preference as
permission to discard fallback findings would lose validated feedback whenever
GitHub rejects inline positions.

## Decision

- Add cascading section settings for summary, issues table, confidence score,
  and diagrams, plus footer visibility.
- Resolve PR-level inclusion with an all-touched-scopes rule, collapse with an
  any-scope rule, and default-open with an all-scopes rule.
- Copy the resolved booleans onto each durable review report and use only that
  snapshot for SCM rendering and retries.
- Apply confidence visibility to the top-level score, issue-table confidence
  column, and inline finding metadata. Keep the underlying confidence value
  durable and available to checks and automatic approval.
- Treat issue tables as optional presentation. Detailed findings remain
  mandatory when inline publication fails or summary-only mode is enabled.
- Keep risk, finding count, and coverage as core review status even if summary
  or confidence sections are hidden.

## Consequences

Operators can configure these output controls without making review
semantics mutable or allowing a cosmetic setting to suppress required safety
feedback. Retry output remains tied to the policy snapshot that produced the
review.

Managed PR/MR-description targeting and agent-fix visibility were added by ADR
0035. Commit-message footer previews, manual diagram selection, and controls
for non-review SCM surfaces remain future work.
