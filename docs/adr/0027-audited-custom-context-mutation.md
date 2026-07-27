# ADR 0027: Audited custom-context mutation

Date: 2026-07-23

Status: Accepted

## Context

Custom context can be active, inactive, or suggested. The MCP surface already
listed, retrieved, searched, and created entries, but operators also need to
edit and delete rules. Diffuse could create operator context but could not
correct, deactivate, or remove it without direct database access.

Blind updates would let two MCP clients silently overwrite one another. Hard
deletion must not erase which context a historical review used, and audit
records should not duplicate potentially sensitive free-form bodies.
Feedback-derived learned rules also have evidence-backed approval and version
semantics that must not be bypassed by a generic context editor.

## Decision

- Add `update_custom_context` and `delete_custom_context` MCP tools behind the
  existing MCP write scope. Leave the four existing list/get/search/create
  custom-context tools unchanged; these are additions.
- Accept only `custom_context_<id>` operator-managed resources. Reject
  `learned_rule_<id>` so learned rules continue through suggestion, evidence,
  edit, approval/rejection, activation, and version history.
- Require `expectedUpdatedAt` from the most recent read for both operations.
  Lock the authorized row and compare timestamps before mutation. A stale
  client fails rather than overwriting newer state.
- Normalize and revalidate type, body, status, path globs, and bounded JSON
  metadata with the same constraints as creation. Require at least one update
  field. Treat an identical update as an idempotent no-op without changing
  `updatedAt` or appending an audit event.
- For a real update, record changed field names, type/status/scope transitions,
  previous/new timestamps, and SHA-256 values for bodies and metadata. Do not
  copy raw bodies or metadata into audit details.
- For deletion, write the authorized audit tombstone and delete the mutable
  operator-context row in one transaction. The tombstone retains identity,
  type/status/scopes, timestamps, and content hashes. Existing
  `review_run_custom_contexts` records are immutable JSON snapshots without a
  foreign key to the live context row and remain reproducible.
- Use the same nonexistent-or-unauthorized response and repository-claim
  intersection as every other MCP object lookup.

## Consequences

Self-hosted operators can complete the full context lifecycle through MCP
without direct SQL, lost updates, or mutable review history. Setting status to
`inactive` is the reversible default; permanent deletion is explicit and
audited.

This does not implement organization Owner/Admin/Member roles. Until RBAC
exists, repository-scoped service tokens with MCP write scope are the mutation
authority. Deleted bodies are intentionally not recoverable from the audit log,
although exact historical review snapshots remain.
