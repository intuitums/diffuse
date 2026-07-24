# ADR 0038: Idempotent REST review trigger

Date: 2026-07-24

Status: Accepted

## Context

REST clients need to request a GitHub or GitLab review without speaking MCP.
A `POST` can time out after durable work was accepted, so blindly retrying can
create multiple review attempts. Process-memory deduplication would disappear
on restart, storing the client key verbatim would retain unnecessary
credential-like material, and fetching provider state again after a crash
could associate one key with two different heads.

The IETF HTTPAPI working group has described the `Idempotency-Key` concept for
fault-tolerant non-idempotent methods and explicitly warns clients not to reuse
a key for a different payload. Its current document is a work in progress, not
a final RFC:

- <https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/>

## Decision

- Add the `POST` review route at
  `/api/v1/repositories/{repository_id}/pull-requests/{number}/reviews`.
  Require both `diffuse:api:read` and `diffuse:api:write`, because the response
  exposes repository and queue state as well as creating work.
- Resolve the numeric repository and open PR/MR under the token's repository
  grant before dispatch. Re-fetch current provider metadata, optionally require
  an exact requested branch, and recheck the branch after the fetch. Queue the
  normalized manual event through the same provider-neutral audited action as
  MCP and return 202.
- Require a documented Diffuse `Idempotency-Key` syntax of 1–200 URL-safe
  visible characters. Store only its SHA-256 digest, scoped by authenticated
  actor identity and operation. Store a canonical request fingerprint; reuse
  with a different repository, PR, or body fails with a stable conflict.
- Reserve the key transactionally with a five-minute execution lease. A live
  concurrent request receives a retryable in-progress response. An expired
  lease can be reclaimed while preserving the reservation timestamp.
- Persist the fully normalized provider event on the reservation before
  enqueueing. A crash after that point reuses the exact delivery, trigger,
  base/head, and metadata rather than fetching potentially newer state.
  Provider delivery deduplication closes the enqueue/response crash window.
- Complete the reservation with the full versioned response. Later identical
  retries bypass provider and queue work, replay the exact JSON, and include
  `Idempotency-Replayed: true`.

## Consequences

API automation can safely retry an uncertain review request without creating
another durable effect. A raw client key is not recoverable from PostgreSQL,
different service tokens have separate key namespaces, conflicting reuse is
visible, and one successful effect produces one audit event.

The implementation intentionally defines a restricted unquoted key syntax
rather than claiming conformance to the evolving Structured Fields draft.
Organization-level rate limits, key-retention cleanup, administrative
inspection, cancellation, and idempotency support for future context/rule
mutations remain. ADR 0039 subsequently applies the same ledger to onboarding
and exact-commit indexing.
