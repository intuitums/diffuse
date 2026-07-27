# ADR 0016: Conservative durable automatic approval

Date: 2026-07-23

Status: Accepted

## Context

Teams need conservative automatic approval of low-risk changes: a default-off
capability that submits an approval only after a clean 5/5 review, an inherent
change-risk check, and configurable filters. Its cascading configuration must be
strictest-wins across every directory a PR touches, rename filters must inspect
both sides, and critical changes must never be approved.

Automatic approval is a higher-impact action than publishing review feedback.
A self-hosted implementation must not equate “the model emitted no findings”
with “safe,” approve a partially reviewed or stale head, let a nested directory
silently weaken root policy, or create duplicate approvals after a worker
crash.

## Decision

- Keep automatic approval disabled by default. The initial publisher is GitHub;
  ADR 0032 subsequently adds GitLab under the same eligibility contract.
- Resolve policy for both old and new paths. Require every touched scope to
  enable approval; use the lowest risk ceiling and file limit; apply every
  inclusion constraint; and reject on any exclusion.
- Require authoritative PR metadata, a complete parseable diff, full review
  coverage with no ignored files, zero current findings/risk, and no active
  finding lineage from earlier commits.
- Classify inherent change risk independently and deterministically. Low covers
  docs/tests/styles and very small changes; ordinary application code is
  medium; dependency/build/runtime/shared-core changes are high.
- Hard-classify auth, public API, secrets, billing/payments,
  schemas/migrations, CI, and infrastructure as critical. Critical changes and
  over-budget diffs are never automatically approved, regardless of the
  configured ceiling.
- Persist the complete immutable decision, policy fingerprint, paths, metrics,
  risk, status, attempts, and remote identity for every requested approval.
- Publish a separate GitHub `APPROVE` review pinned to the exact commit. Before
  posting, re-fetch the PR and require it to remain open, non-draft, and on the
  reviewed head.
- Recover the remote-create crash window by searching for a hidden per-run,
  per-head marker. Cancel stale-head approval instead of retrying it.

## Consequences

Diffuse can remove human waiting time for a deliberately narrow class of
changes without allowing review silence, path ignores, stale commits, or
permissive nested configuration to become approval authority. Operators can
audit why any run was or was not approved.

This remains a foundation. Dashboard and organization policy, installation-
scoped approval credentials, versioned risk evals, analytics, and an operator
kill switch remain outstanding. ADR 0032 supplies the GitLab publisher.
