# ADR 0033: Provider-neutral MCP manual review triggering

Date: 2026-07-23

Status: Accepted

## Context

ADR 0023 introduced a write-scoped MCP review trigger, but its authoritative
metadata fetch and target guard supported only GitHub. ADR 0032 added GitLab
manual review behavior through top-level MR comments, leaving an MCP client
unable to request the same review for an authorized GitLab repository.

The public MCP repository descriptor includes a provider and host. Accepting
either as an unchecked fetch target would create an authorization and
server-side request boundary. GitLab also identifies API resources by numeric
project ID while Diffuse and MCP use a slash-separated namespace, including
nested groups.

Relevant public contracts:

- <https://www.greptile.com/docs/mcp-v2/tools>
- <https://docs.gitlab.com/api/projects/>
- <https://docs.gitlab.com/api/merge_requests/>

## Decision

- Keep the public `trigger_code_review` input and response shape provider
  neutral. Support registered GitHub and GitLab repositories without adding a
  second provider-specific tool.
- Resolve `name`, `remote`, `defaultBranch`, optional `remoteUrl`, and PR/MR
  number through PostgreSQL under the caller's repository claims before any
  provider request. Reject closed targets and an initial branch mismatch.
- Derive the API endpoint from the resolved registered host. Use the explicitly
  configured API URL for the configured primary instance and the provider's
  conventional enterprise/self-managed API path for another registered host.
- For GitHub, fetch the current PR through the existing authoritative
  normalization path.
- For GitLab, URL-encode the complete nested namespace and fetch the project
  first. Require its numeric ID, exact `path_with_namespace`, and canonical
  project web URL to match the authorized target. Then fetch the current MR and
  reuse GitLab's authoritative base/head/start, lifecycle, fork-project, and
  changed-file normalization.
- Require the provider response to remain open and recheck an optional requested
  branch against the freshly fetched head branch.
- Give each successful invocation a new bounded MCP delivery and manual trigger
  identity. Enqueue the exact current revision through the existing
  provider/host/repository-checked workflow and record the scoped token/operator
  actor in `code_review.triggered`.

## Consequences

An MCP client with write scope can now re-run a current GitHub PR or GitLab MR
through one contract. Nested GitLab groups and self-managed instances work
without trusting caller-provided project IDs or arbitrary API URLs. The
workflow retains same-head supersession, durable audit, provider/host-scoped
identity, and commit-pinned review behavior.

Each GitLab invocation adds a project lookup before the MR metadata read. UI
re-trigger controls, organization/team RBAC, generation budgets, and scheduled
report export remain separate parity work.
