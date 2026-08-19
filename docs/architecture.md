# Diffuse architecture

A map for navigating the codebase. Read [v1-scope.md](v1-scope.md) first for
the product boundary; this page explains how the code is organized and where
each responsibility lives.

## Runtime boundaries

Diffuse keeps four packages under `packages/`. The server's importable package
is `diffuse` (not `diffuse_server`); the other three follow the `diffuse_*`
prefix convention:

| Package | Import name | Role |
| --- | --- | --- |
| `server` | `diffuse` | Self-hosted API, worker, CLI, review flow, repository data |
| `host` | `diffuse_host` | Credential-isolated Codex / Claude execution |
| `protocol` | `diffuse_protocol` | Wire contracts shared by server and hosts |
| `relay` | `diffuse_relay` | Public GitHub event and short-lived credential edge |

The worker never executes a vendor CLI or receives vendor credentials. An
isolated host reaches the control plane only through the narrow proxy in
`diffuse/investigation/context_service.py`, which forwards one enumerated
capability route; the host holds no database or operator token.

## Review lifecycle

```text
GitHub webhook / delivery poll
  api/app.py  ->  github/delivery_poller.py  ->  worker.py
      -> review/workflow.py        (queue + webhook idempotency)
      -> review/request.py         (build immutable agent request)
      -> review/engine.py + review/agents.py + review/agent_client.py
              (dispatch to host via investigation/context_service.py)
      -> review/report_assembly.py + review/provenance.py + review/lineage.py
      -> github/review_publish.py  (idempotent Check / PR comment)
  database/review_store.py        (persistence throughout)
```

`review/trigger.py` and `review/workflow.py` own scheduling and
idempotency; `review/engine.py` is the agent contract boundary (Diffuse has no
direct model client).

## Where responsibilities live (noun collisions)

Several concepts repeat across layers with different suffixes. This table is
the index:

| Concept | Location | Responsibility |
| --- | --- | --- |
| Review orchestration | `diffuse/review/` | Build request, queue, dispatch to host, validate, assemble |
| Publish review to GitHub | `diffuse/github/review_publish.py` | Idempotent Check / PR-comment publication |
| Review persistence | `diffuse/database/review_store.py` | ORM models for runs, findings, publications |
| Operator review CLI | `diffuse/cli/review.py` | Local-branch review |
| Context for the agent host | `diffuse/investigation/context_api.py`, `context_service.py` | Tool surface + proxy to the isolated runner |
| Retrieval context | `diffuse/repository/retrieval/retrieve.py`, `context_models.py` | PR-diff context + immutable snapshot plan |
| Custom context | `diffuse/database/custom_context.py` | Operator-managed context rows |

"Context" is overloaded on purpose and means different things per layer:
*investigation context* (tools/snapshot exposed to the host),
*retrieval context* (the PR-diff plan), *custom context* (operator-authored
rows), and *request context* (the immutable plan handed to a Review Agent).

## Removed surfaces

Diffuse previously supported GitLab, a public MCP/REST surface, and automatic
approval. Those were removed; this codebase is GitHub-only with no MCP service
or auto-approval. Historical rows or queued payloads shaped by those surfaces
are no longer accepted, and the `POLICY_SCHEMA_VERSION` sentinel no longer
references them.
