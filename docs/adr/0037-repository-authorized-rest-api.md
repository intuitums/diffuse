# ADR 0037: Repository-authorized versioned REST API

Date: 2026-07-24

Status: Accepted

## Context

Diffuse has durable repository, index, pull-request, review, finding,
analytics, and source-query state, but those projections were available only
to MCP clients and local operators. A web control plane and ordinary
integrations need a versioned HTTP contract. Building a second token verifier
or filtering repositories only in route handlers would create authorization
drift and make one missed filter a cross-repository data leak.

Plain search reads an existing immutable index, while repository Q&A invokes a
generation model. A generic read credential must not implicitly authorize that
cost or data-processing action.

Relevant specification:

- <https://www.rfc-editor.org/rfc/rfc9457>

## Decision

- Mount the first native control-plane contract at `/api/v1` and publish it
  through the application OpenAPI document. Preserve the existing durable
  projection payloads and add `apiVersion: "v1"` at the transport boundary.
- Centralize recovery-credential and durable service-token authentication in
  one shared module used by MCP and REST. Keep constant-time recovery-token
  comparison, hashed durable credentials, expiration/revocation, last-used
  tracking, and bounded repository grants.
- Require `diffuse:api:read` for repository/index, pull-request, review,
  finding, analytics, and source-search routes. Add
  `diffuse:api:generate` through migration 2 and require both scopes for
  model-backed Q&A. Administrative scope satisfies either requirement;
  no write scope authorizes an unspecified mutation. ADR 0038 subsequently
  activates read-plus-write scope for the manual review-trigger route, and ADR
  0039 adds administrative onboarding plus repository-scoped reindexing.
- Pass the authenticated repository grant to the same PostgreSQL projection
  functions used by MCP. Resolve numeric repository IDs inside that boundary.
  Return the same 404 for a nonexistent and unauthorized object.
- Bound page sizes, path IDs, time windows, text fields, source counts, and
  request-body keys before doing data or model work. Keep blocking PostgreSQL,
  retrieval, and model calls off the event loop.
- Use RFC 9457-style `application/problem+json` responses for authentication,
  authorization, validation, data-store, and generation failures without
  returning credentials, SQL details, or unauthorized resource identity.

## Consequences

Web UI work and ordinary integrations can now inspect Diffuse without speaking
MCP or receiving installation-wide access. Repository isolation and token
lifecycle have one implementation across both protocols, and a read-only
credential cannot spend model capacity.

The initial surface is intentionally read-oriented. ADR 0039 subsequently
adds idempotent repository onboarding and indexing. Context/rule administration,
organization/team RBAC, rate and usage accounting, outbound webhooks, cursor
pagination, compatibility guarantees, and a generated client SDK remain
future work.
