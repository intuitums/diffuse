# ADR 0022: Scoped non-recoverable service tokens

Date: 2026-07-23

Status: Accepted

## Context

The initial MCP boundary used one environment-projected credential with access
to every repository. That is useful for bootstrap and recovery, but routine IDE
and automation clients need durable revocation, expiration, least-privilege
repository access, and an inspectable operator trail.

Recoverable credentials or repository filtering only in individual tools would
create unnecessary secret exposure and make one missed query an authorization
bypass.

## Decision

- Keep `DIFFUSE_API_TOKEN` as an installation-wide bootstrap/recovery
  credential. Validate it in constant time and grant the documented bootstrap
  scopes only in request context.
- Store durable service credentials as SHA-256 digests only. Require 32–512
  visible ASCII characters so offline guessing is impractical when operators
  generate credentials with a cryptographically secure source.
- Model explicit scopes, optional expiration, revocation metadata, and either
  all-repository access or a bounded repository allowlist.
- Require `diffuse:mcp:read` for the current MCP surface. Reserve MCP/API write
  and admin scopes for future authenticated surfaces rather than treating a
  read credential as implicitly writable. ADR 0023 subsequently activates MCP
  write scope, and ADR 0026 adds a separate generation scope for model-backed
  repository Q&A.
- Carry repository authorization in the verified access-token context and add
  the constraint to every MCP list, search, and object lookup in the
  PostgreSQL projection layer. Missing and unauthorized object lookups use the
  same external error.
- Create, list, and revoke credentials through `diffuse token`. Creation reads
  the credential only from a named environment variable; command output and
  list records contain neither the credential nor its digest.
- Append immutable token creation and revocation events with actor, resource,
  and safe scope metadata.

## Consequences

Compromise of one routine MCP credential is limited to its declared
repositories and read scope. Operators can revoke it without restarting the
service, and the database cannot recover the bearer value.

The bootstrap credential remains intentionally powerful and must stay in a
secret manager. Organization/team ownership, interactive identity, role
inheritance, credential rotation workflows, administrative APIs, and broader
audit/export governance remain future work.
