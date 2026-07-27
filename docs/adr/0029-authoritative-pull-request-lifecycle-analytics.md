# ADR 0029: Authoritative pull-request lifecycle analytics

Date: 2026-07-23

Status: Accepted

## Context

An analytics surface needs PRs reviewed, average time from PR open to merge,
addressed rate, critical findings, reaction ratios, and repository, author,
team, and time filters, plus total PRs reviewed versus unreviewed. Diffuse's
first analytics projection could
report review and finding facts, but its PR row held only creation and latest
event times. Treating generic update or webhook-receipt time as merge time
would create a plausible but false metric. A mutable current-state row would
also lose close/reopen history.

GitHub PR resources carry `created_at`, `closed_at`, and `merged_at`. GitLab
merge-request webhooks carry `created_at`, `actioned_at`, and, on versions that
provide it, `merged_at`. A provider can omit a field, especially across
self-managed versions, so missing data must remain visible.

Public references:

- <https://docs.github.com/en/rest/pulls/pulls>
- <https://docs.gitlab.com/user/project/integrations/webhook_events/>

## Decision

- Extend the provider-neutral `PullRequestEvent` with optional authoritative
  source-created, source-closed, and source-merged timestamps. Normalize
  explicit timezones to UTC, reject terminal timestamps on an open event and a
  merge timestamp on a merely closed event, and reject terminal times before
  source creation.
- Require GitHub normalization to read `created_at` and retain `closed_at` and
  `merged_at` when present. Manual review metadata fetched from the GitHub API
  follows the same path.
- Keep nullable authoritative timestamps on the current `pull_requests`
  projection. Preserve a known creation timestamp across later partial events;
  replace or clear terminal timestamps according to current state. Never
  substitute `updated_at`, receipt time, or database time for a missing source
  lifecycle timestamp.
- Append every accepted onboarded PR delivery to
  `pull_request_lifecycle_events`, keyed to the durable webhook delivery.
  Duplicate deliveries cannot duplicate events. A chronologically stale event
  is retained in the ledger but cannot replace current PR state. Each event
  records action, state, source action time, and every supplied lifecycle
  timestamp.
- Expose current `closedAt` and `mergedAt` through the MCP PR projection.
- Extend `get_review_analytics` with an optional case-insensitive exact author
  filter under the existing repository claims.
- Define the opened cohort as PRs whose authoritative source-created timestamp
  is in the half-open report window. Count a cohort PR as reviewed only when a
  published review also started in that window; return the reviewed,
  unreviewed, and current-state cohort counts and the exact denominator.
- Define merge-time samples as PRs whose authoritative source-merged timestamp
  is in the window and whose authoritative source-created timestamp is known.
  Return mean, median, sample count, UTC daily trends, distinct merge events,
  and the fraction carrying exact merge timestamps.
- Add current positive/negative reaction percentages. Keep historical
  policy-eligible coverage, team filtering, and monetary cost explicitly
  unavailable until their versioned inputs exist.

## Consequences

Diffuse can produce PR-reviewed and merge-time summary cards from authoritative
SCM facts while showing how complete those facts are. Close,
reopen, stale-delivery, and merge history survives later state changes, and
repository/author filters cannot widen a token's repository grants.

Older persisted PRs and providers that omit merge timestamps do not become
fake samples. Their missing timestamps reduce the reported completeness rate.
GitLab webhook ingress, team/RBAC filters, historical policy-eligibility
snapshots, dashboard charts, scheduled delivery, and export remain future
work.
