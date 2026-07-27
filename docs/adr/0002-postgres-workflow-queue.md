# ADR 0002: PostgreSQL-backed durable workflow queue

Date: 2026-07-23

Status: Accepted

## Context

Acknowledging an SCM webhook before an in-process background task finishes does
not make the work durable. A process restart loses accepted reviews, repeated
deliveries can publish duplicate comments, and delayed events can review an old
PR head after a newer revision arrives.

Small self-hosted installations already require PostgreSQL, so requiring a
second workflow system at the foundation stage would add operational cost
before the workflow domain is stable.

## Decision

Diffuse begins with a PostgreSQL-backed workflow queue.

- SCM deliveries are identified by provider, base URL, and delivery ID.
- The raw request is represented only by its SHA-256 digest; secrets and source
  payloads are not copied into routine delivery records.
- Pull requests retain the latest provider event time and exact base/head SHAs.
- A review's idempotency key is its provider, host, repository, PR number, base
  SHA, and head SHA.
- Delivery recording, PR state advancement, deduplication, supersession, and
  job creation happen in one transaction under a scope advisory lock.
- Out-of-order events cannot replace a newer recorded PR head.
- Workers claim available jobs with row locking and `SKIP LOCKED`.
- Each claim creates an attempt and a time-bounded lease.
- Expired leases are recoverable; retry delay grows exponentially to a bounded
  maximum; exhausted work enters a dead state.
- Workers re-check the current PR head before every externally visible review
  stage.

## Consequences

The API process can restart immediately after returning `202` without losing
accepted work, multiple workers can claim independently, and common webhook
retries do not duplicate reviews.

PostgreSQL polling is intentionally a small-installation design. Scheduling,
per-tenant limits, operator replay, and operator visibility remain required.
ADR 0004 subsequently separated native generation from publication and added
heartbeats, supersession checks, and marker-backed publication recovery.
