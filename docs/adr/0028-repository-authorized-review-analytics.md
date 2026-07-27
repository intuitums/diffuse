# ADR 0028: Repository-authorized review analytics

Date: 2026-07-23

Status: Accepted

The unavailable lifecycle metrics in this decision are superseded by
[ADR 0029](0029-authoritative-pull-request-lifecycle-analytics.md).

## Context

Teams need analytics reports assembled from the MCP review, pull request,
comment, repository, and custom-context tools. Those reports cover review
completion, response time, addressed findings, category/severity distribution,
critical and security findings, reactions, custom-context adoption,
per-repository activity, weekly trends, and usage.
Diffuse persisted most of those source facts but had no bounded analytics
projection. Asking each client to reconstruct denominators independently would
produce inconsistent rates and could bypass repository-scoped service-token
authorization.

Diffuse does not yet persist a complete set of PRs eligible for review, an
exact merge timestamp, or versioned model-provider prices. Reporting review
coverage, merge time, or historical monetary cost from the available columns
would invent precision.

## Decision

- Add the read-scoped `get_review_analytics` MCP tool. Keep the raw underlying
  projections available so general MCP clients can still compose their own
  reports.
- Require timezone-aware `startAt` and `endAt`. Treat the interval as half-open,
  normalize it to UTC, and reject non-positive intervals or windows longer than
  366 days.
- Accept an optional repository descriptor. When omitted, aggregate only
  repositories assigned to the service token. Resolve descriptors and apply
  repository claims in PostgreSQL exactly as other MCP reads do; unauthorized
  inputs remain indistinguishable from nonexistent repositories.
- Define review completion as published runs divided by all runs started in the
  window. Count attempts with distinct review identities so joining findings
  cannot multiply the denominator. Report each terminal/in-progress status,
  distinct PRs, published latency, tokens, auto-approvals, and published runs
  with immutable custom-context snapshots.
- Select findings only when they have an applied lineage event in a published
  run started in the window. Report occurrence and unique-lineage counts,
  present addressed/active/critical/security state, first-address latency,
  category/severity groups, and at most twenty priority-ordered open findings
  linked to their repository and PR.
- Derive current reaction state from the newest durable event for each external
  reaction, so a withdrawal cancels its prior observation. Count context
  replies separately. Engagement-rate denominators are selected unique
  lineages, not comment events.
- Return repository breakdowns and UTC daily buckets. Label the current-state
  projection with the database transaction `asOf` timestamp, define every
  denominator in the response, and return `null` for a zero denominator.
- Explicitly list review coverage, merge time, and monetary cost as unavailable
  with the missing durable fact instead of estimating them.

## Consequences

Operators and MCP clients receive one deterministic, least-privilege reporting
contract over the same durable facts used by reviews and workers. A run with
many findings cannot inflate review volume, and reaction withdrawals cannot
inflate engagement.

Current-state fields can legitimately change after `asOf` as later commits or
feedback update a selected lineage. Scheduled weekly delivery, CSV/JSON export,
dashboard visualization, exact eligible-PR coverage, exact merge-time
analytics, and versioned monetary cost remain future work.
