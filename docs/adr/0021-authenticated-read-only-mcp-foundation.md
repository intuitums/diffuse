# ADR 0021: Authenticated read-only MCP foundation

Date: 2026-07-23

Status: Accepted; authentication boundary extended by ADR 0022. The read-only constraint recorded here is superseded: ADR 0023 introduced write-scoped MCP tools including `create_custom_context`, and ADR 0027 added `update_custom_context` and `delete_custom_context`. The MCP server is no longer read-only; everything else in this decision still holds.

## Context

Coding agents need a bearer-authenticated Streamable HTTP MCP server with tools
for pull requests, reviews, comments, search, and custom context. Diffuse had
durable data for several of those outcomes but no authenticated developer
protocol beyond signed SCM webhook ingress.

Advertising tools that infer missing pull-request state, mutate rules through
an unrelated learned-rule path, or trigger reviews without authoritative SCM
metadata would create a misleading and unsafe tool surface.

## Decision

- Use the production-stable `<2` line of the official Python MCP SDK; v2 is
  still alpha.
- Mount stateless JSON Streamable HTTP at `/mcp` inside the existing ASGI
  service and run the SDK session manager in the application lifespan.
- Require a pre-provisioned `DIFFUSE_API_TOKEN` with at least 32 characters,
  compare it in constant time, require the MCP read scope, and store or
  log neither the token nor source/model output.
- Keep DNS-rebinding protection enabled with an explicit host allowlist and a
  separately configured public origin.
- Initially advertise only seven read-only tools backed by durable state:
  repository discovery; review list/detail; current published finding
  lineages per PR; cross-review finding search; and learned-context
  list/detail with evidence and moderation history.
- Use bounded pagination, validated identifiers, escaped literal substring
  search, stable resource ID prefixes, and sanitized database failure errors.
- Treat the single token as an operator-wide authorization boundary for the
  current single-tenant self-hosted foundation.

## Consequences

MCP-compatible coding agents can inspect self-hosted Diffuse review state
without a hosted control plane. The tool list does not claim unsupported write
or PR-state behavior.

ADR 0022 adds repository-scoped service tokens while retaining this
pre-provisioned credential as a recovery path. At this ADR's acceptance, the
capability ledger still required organization/team RBAC, OAuth discovery,
pull-request state/listing, review trigger/re-run, finding resolution,
custom-context mutation, code search, fix handoff, and analytics/report tools. ADRs 0023–0026
subsequently establish working foundations for several of those surfaces; RBAC,
OAuth discovery, verified mutation flows, and complete analytics remain.
