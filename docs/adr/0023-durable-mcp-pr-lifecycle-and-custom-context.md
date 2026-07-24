# ADR 0023: Durable MCP pull-request lifecycle and custom context

Date: 2026-07-23

Status: Accepted

## Context

Greptile's public MCP surface connects pull-request discovery, review
triggering, findings, and custom context. Diffuse already exposed review
outcomes, but could not safely list PR lifecycle state, re-run a review from an
IDE, or create context that actually influenced future reviews.

Treating MCP input or the last webhook payload as authoritative for a re-run
would risk reviewing a stale commit. Storing context without incorporating it
into immutable review provenance would make the tool appear successful while
leaving review behavior unchanged.

Public references:

- <https://www.greptile.com/docs/mcp/overview>
- <https://www.greptile.com/docs/mcp/tools>

## Decision

- Persist open, closed, and merged pull-request state plus source creation
  time and change statistics. A closed/merged event records the terminal state,
  cancels queued review work, and makes running work fail its current-state
  check.
- Expose repository-authorized pull-request list and detail projections with
  review and current finding counts.
- Require `diffuse:mcp:write` or the administrative scope for review triggers
  and custom-context creation. Read scope remains sufficient for every
  inspection/search tool.
- Before a GitHub re-run, fetch current PR metadata from the configured SCM,
  reject non-open PRs or a mismatched requested branch, then enqueue the exact
  base/head revision through the existing idempotent workflow contract. ADR
  0033 extends this contract to GitLab without weakening repository scope.
- Store operator-managed context separately from feedback-derived learned
  rules. Context creation is repository-scoped and audited, with bounded body,
  JSON metadata, and path glob validation.
- Combine operator context and learned rules in MCP list/detail/search without
  erasing their distinct provenance.
- Apply only active operator context to matching changed paths, beneath
  repository-authored guidance. Include it in the resolved policy fingerprint
  and snapshot its exact body, scopes, type, and metadata on the review run.

## Consequences

An MCP-compatible IDE can move from PR discovery to a fresh durable review and
can create standards that affect subsequent reviews without bypassing source
control or authorization boundaries. Historical review provenance remains
stable even if context later changes or is removed.

Organization/team context remains future parity work. Public repository
descriptor and comment-tool compatibility is addressed by ADR 0024;
revision-safe agent handoff is addressed by ADR 0025; audited
update/deactivate/delete operations are addressed by ADR 0027; provider-neutral
GitHub/GitLab MCP review triggering is addressed by ADR 0033.
