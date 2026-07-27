# ADR NNNN: Short imperative title

Date: YYYY-MM-DD

Status: Proposed

<!--
Status is one of:
  Proposed   - under discussion, not yet binding
  Accepted   - binding; the code should match this
  Superseded by ADR NNNN - kept for history, no longer binding
  Rejected   - considered and declined; keep it, the reasoning is the value

Amending an accepted record: append to its Status line, do not rewrite its
Context or Decision. Numbers are never reused and records are never deleted.
-->

## Context

What is true today that forces a decision. Constraints, the failure mode being
avoided, and any measurement that motivated it — a number, a reproduction, a
file and line. Not a feature description.

## Alternatives considered

The part most worth writing down, and the part most often skipped.

- **Option A** — why it lost.
- **Option B** — why it lost.

If nothing was seriously weighed, say so plainly rather than inventing
alternatives. "No alternative was considered; this was the obvious shape" is
honest and useful. A fabricated comparison is worse than none.

## Decision

What we are doing, in the present tense, specifically enough to check the code
against. Name the modules, tables, or contracts it binds.

## Consequences

What this makes easy, what it makes hard, and what it commits us to. Include the
costs — an ADR that lists only benefits was not a decision.

State any migration this forces. A change to the policy models or to
`policy_fingerprint` requires bumping `POLICY_SCHEMA_VERSION`; a change to
language adapters or grammars requires bumping `LANGUAGE_ADAPTER_SCHEMA_VERSION`.
Both invalidate existing index snapshots and require
`diffuse repository sync --all`.
