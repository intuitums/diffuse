# Agents

> **Status: active v1 direction.** This page replaces the old “agent runtime”
> plan. When code or an older document conflicts with it, follow
> [v1-scope.md](v1-scope.md).

Diffuse owns GitHub webhook ingestion, commit-pinned context, orchestration,
verification, lineage, and publication. Codex and Claude Code are isolated,
read-only **Agents**. They investigate one exact pull-request head
and return structured candidate findings; they never publish to GitHub or hold
Diffuse control-plane credentials.

## Boundary

```text
Diffuse worker
  -> isolated candidate Agent Host (Codex or Claude Code)
  -> structured candidate findings
  -> isolated verifier Agent Host (the other engine)
  -> deterministic validation, lineage, and publication
```

The worker never executes a vendor CLI and never mounts a vendor credential.
Each runner has only its own credential home, a read-only review workspace,
bounded vendor egress, and private access to the exact immutable context it was
issued. It has no database, GitHub App, operator API token, branch-write, or
arbitrary-network access.

The transport may use a narrowly scoped internal protocol, but it is not a
public MCP server and is not a product integration surface. It must expose only
the context operations required for a review investigation.

## Investigation contract

An investigation is bound to one repository, pull request, index snapshot, and
head SHA. It has a strict timeout, bounded work budget, role, and read-only
operation allowlist.

Its output is a **candidate**, not a final review. Every candidate must name:

- the exact location and code evidence;
- severity and category;
- why the change fails or is risky; and
- the relevant head SHA and investigation identity.

A separate investigation on the other engine verifies the canonical candidate
result and can reject any candidate. The verifier is bound to the candidate
result digest; omitted decisions reject. Diffuse performs final exact-head
validation, deduplication, lineage changes, and GitHub publication.

## Delivery sequence

1. Keep the runner isolation boundary and private, exact-head context access.
2. Keep configuration and operator language aligned on Review Agents, Agent
   Hosts, Investigations, and Review Access Grants.
3. Complete one end-to-end Codex review engine, including verification and
   publication. Add Claude through the same contract.
4. After evidence from the single-engine path, add the bounded correctness,
   security, and integration team plus a verifier.

## Transitional implementation

Direct model API execution is retired. Reviews use the Agent Host contract;
provider routing, public MCP, conversation, and Q&A are not part of v1.

### Current private transport foundation

The worker creates a deterministic, bounded source artifact from the exact
checked-out head and signs both its transport digest and canonical workspace
digest into the private dispatch. The runner validates and materializes that
artifact as a read-only workspace before starting the CLI; it never clones,
mounts a repository mirror, or receives SCM credentials. The in-envelope
foundation is limited to a 16 MiB source archive, a 128 MiB extracted tree, and
one active review per 1 GiB runner while larger delivery moves to object-backed
transport.

Scoped private context access is bound to the repository, pull request,
snapshot, head, review attempt, exact bearer digest, and capability lifetime.
The candidate and verifier have separate durable Investigations, capabilities,
budgets, result digests, lifecycle states, and token accounting.

`REVIEW_AGENT` selects the candidate engine. Its values are `codex` and
`claude`; the other engine verifies, so both authenticated Agent Hosts must be
running. Both execute only through an isolated Agent Host. Session capabilities
are the internal representation of a Review Access Grant, not a public API.
