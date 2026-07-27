# ADR 0025: Revision-safe agent fix handoffs

Date: 2026-07-23

Status: Accepted

## Context

Developers need one-finding and Fix All handoffs to Claude Code, Codex,
Conductor, Cursor, and Devin. A handoff carries file locations, review comments,
and suggested fixes; a local bridge opens the selected agent. Diffuse already
stored that review data, but asking an agent to act on an old review can edit
the wrong revision. Suggested fixes and repository text are also untrusted model
and source input, not executable instructions.

## Decision

- Add read-scoped `get_fix_handoff` and `get_fix_all_handoff` MCP tools for
  Codex, Claude Code, Conductor, Cursor, Devin, and generic MCP clients.
- Resolve the review under the authenticated token's repository claims and
  reveal the same error for an unauthorized or nonexistent review.
- Return a handoff only for a published review on an open PR whose stored base
  and head still exactly match current durable PR state.
- Include only active lineages whose latest applied occurrence is the finding
  in that review. An addressed, reopened-on-a-newer-review, or otherwise
  superseded occurrence cannot be handed off from stale data.
- Include repository/PR/review identity, exact base/head, file/line/side,
  category/severity/confidence, evidence, suggested fixes, external comment
  IDs, and immutable learned-rule/custom-context snapshots.
- Render a deterministic prompt that labels all review and repository data as
  untrusted, requires exact checkout verification, requires minimal justified
  changes and relevant tests, and forbids automatic commit, push, or remote
  thread resolution.
- Put the exact Fix One call in new inline comments and the Fix All call in the
  review body. Preserve the hidden finding marker even when GitHub's inline
  body limit truncates human-readable content.

## Consequences

Any MCP-capable agent can receive a complete, current, source-linked fix
request without Diffuse gaining write access to the user's checkout. A pushed
fix is evaluated through the normal subsequent review and finding-continuity
workflow instead of trusting the agent's success claim.

This is not yet the literal browser-to-local one-click experience. An optional
local bridge still needs custom URL registration, authenticated handoff
retrieval, checkout selection, per-user agent configuration, and explicit
launch confirmation.
