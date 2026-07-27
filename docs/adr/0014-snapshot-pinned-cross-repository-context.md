# ADR 0014: Snapshot-pinned cross-repository context

Date: 2026-07-23

Status: Accepted

## Context

Reviews need repository-authored `context.repos` and reusable repository
clusters so that a review can read read-only context from related
repositories. A self-hosted implementation must not turn an `owner/repo`
string into an unrestricted fetch, mix hosts or credentials, let a moving
active index change evidence during one review, or collapse equal paths from
different repositories into one retrieval candidate.

## Decision

- Support cascading `context.repos` in immutable repository policy and
  operator-managed repository clusters.
- Require every related repository to be explicitly onboarded, enabled, and on
  the same SCM provider and exact base host as the primary repository.
- Give explicit repository configuration precedence over cluster membership.
  Deduplicate the union and cap it at seven related repositories.
- Fail closed when an explicit repository is missing, disabled, or lacks a
  compatible active snapshot. Skip an unavailable cluster-only member.
- Resolve an ordered immutable plan before review generation. Pin the primary
  and each related repository to exact snapshot and commit identities, include
  the plan fingerprint in review idempotency, and snapshot related provenance
  on the review run.
- Embed the review query once. Use graph, lexical, and semantic retrieval for
  the primary snapshot, but only read-only lexical and semantic retrieval for
  related snapshots until explicit cross-repository graph contracts exist.
- Include repository identity in fusion keys and rendered provenance. Never
  allow related context to become an inline finding location; findings remain
  grounded to changed primary-repository lines.
- Treat authenticated operator shell access as the initial cluster-management
  authorization boundary.

## Consequences

Reviews remain reproducible while related indexes continue updating, path
collisions cannot erase provenance, and committed explicit dependencies cannot
silently disappear. The feature provides useful shared-library and SDK context
without inventing graph relationships that the index does not contain.

Organization/team RBAC, encrypted installation-scoped credential checks,
dashboard/API management, cross-repository graph edges, conversations using
the multi-repository plan, and measured cross-repository recall remain
outstanding.
