# ADR 0024: Descriptor-addressed MCP contract boundary

Date: 2026-07-23

Status: Accepted; amended twice. First, the legacy comment-search alias and its compatibility response field were removed from the server: `search_review_comments` and `diffuseGenerated` are the only names Diffuse exposes. Second, ADR 0041 made GitHub the only supported provider, which voids the GitLab half of two decisions below. `McpRemote` is now `Literal["github"]` and an omitted `remoteUrl` can only default to the public GitHub origin. The dual `list_merge_requests`/`list_pull_requests` naming survives for compatibility but no longer has a GitLab rationale, and `get_merge_request` and `list_merge_request_comments` still have no pull-request-named equivalent. Renaming them is unresolved.

## Context

Diffuse's initial MCP tools used numeric repository IDs and snake_case
parameters. That is convenient for the internal database, but an MCP client
knows a repository by its human-readable coordinates, not by a Diffuse row ID,
and MCP tool schemas are conventionally camelCase. Diffuse's MCP contract
therefore addresses a repository by `name`, `remote`, `defaultBranch`, and
optional `remoteUrl`, and uses camelCase fields such as `prNumber`.

Merely advertising aliased JSON Schema properties is insufficient. FastMCP
invokes the Python function using the wire-name keyword, so an alias can appear
valid during discovery and still fail when called. Repository descriptor
resolution must also enforce token claims without revealing whether an
unauthorized repository exists.

## Decision

- Keep camelCase wire names in thin MCP boundary functions; translate
  immediately to Diffuse-native snake_case storage calls. Accept the snake_case
  spelling as an alias so both conventions resolve to the same parameter.
- Advertise and execute both `list_merge_requests` and `list_pull_requests` so
  GitLab-oriented and GitHub-oriented callers can each use their own vocabulary
  against the same durable projection.
- Resolve a complete repository descriptor to an internal repository ID in the
  same database query that applies the authenticated token's repository claims.
  Reject partial descriptors, mixed numeric/descriptor identity, and
  unauthorized or nonexistent targets with the same error.
- Default an omitted `remoteUrl` to the public GitHub or GitLab origin, while
  requiring it for a different self-hosted origin to resolve.
- Expose `list_merge_request_comments` from the latest applied finding in each
  durable lineage and include the external root comment ID when one exists.
- Expose `search_review_comments` as the organization-wide finding search across
  reviews, with optional repository filtering and pagination.
- Name the generated-finding filter and response field `diffuseGenerated`,
  accepting `generated` as an alias, so finding provenance is unambiguous.
- Continue deriving addressed state from published finding-lineage transitions.
  MCP does not claim that changing a response field or applying a local edit has
  resolved the remote review thread.

## Consequences

MCP-capable agents can drive PR, review, and comment tools against a
self-hosted Diffuse server using descriptor-addressed, camelCase parameter
shapes. The boundary does not duplicate review data, bypass repository scopes,
trust a stale branch name, or add any external service as a runtime dependency.

Organization-scoped implicit context creation is still unavailable because
Diffuse does not yet have the organization/team RBAC model. First-class
analytics remain future work at this ADR's acceptance. Revision-safe agent
handoff is addressed by ADR 0025; source-linked search and grounded repository
Q&A are addressed by ADR 0026; audited operator-context mutation is addressed by
ADR 0027; provider-neutral GitHub/GitLab MCP review triggering is addressed by
ADR 0033.
