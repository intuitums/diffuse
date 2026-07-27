# ADR 0039: Idempotent repository onboarding and indexing API

Date: 2026-07-24

Status: Accepted

## Context

Repository onboarding and default-branch refresh existed only as local
operator CLI actions. A web control plane and automation need ordinary HTTP
mutations, but repository creation cannot be authorized by a grant to a
not-yet-existing numeric ID. Clone/fetch may outlive a short request retry
window, provider credentials must not be offered to a caller-selected host, and
a retry after resolving the branch must not silently index a newer commit.

The onboarding outcome operators expect is connecting a code provider,
selecting enabled repositories, and beginning indexing in the background.
Diffuse implements that outcome within its existing self-hosted credential and
workflow boundaries.

## Decision

- Add `POST /api/v1/repositories` for one repository and
  `POST /api/v1/repositories/{repository_id}/indexes` for a default-branch
  refresh. Both return 202 only after an exact commit has entered the durable
  queue.
- Require `diffuse:admin` plus all-repositories access for creation. Require
  `diffuse:api:read`, `diffuse:api:write`, and the repository grant for
  reindexing. Do not let a repository-scoped token create an object outside its
  claims.
- Accept only the provider's configured primary origin or an exact origin in
  `GITHUB_ALLOWED_INSTANCES`/`GITLAB_ALLOWED_INSTANCES`. Derive the
  credential-free clone URL from validated provider, origin, namespace, and
  repository name. Reject an existing disabled or differently configured
  identity instead of silently rewriting it.
- Resolve the current default-branch commit through the same locked bare mirror
  and non-interactive askpass path as workers and the CLI. Keep clone/fetch off
  the event loop. Record visible `syncing`, `ready`, or failed mirror state.
- Reuse the actor/operation-scoped hashed idempotency ledger from ADR 0038.
  Repository operations use a bounded 30-minute initial lease because an
  initial clone plus a confirming fetch may exceed the review trigger's
  five-minute lease. Persist the fully
  normalized `PushEvent` before enqueueing so a reclaimed lease reuses its
  exact commit, timestamp, and deterministic delivery ID.
- Enqueue through the same transactional provider/host/repository-ref workflow
  as authenticated push webhooks. Revision and delivery deduplication,
  queued-job supersession, worker checkout verification, and immutable
  snapshot activation remain single implementations.
- Audit repository creation once and each newly accepted index delivery once.
  A duplicate delivery used for crash recovery does not append another audit
  event. Store and replay the exact completed response.

## Consequences

An operator, future web UI, or automation client can safely onboard and refresh
repositories without shell access. Credentials cannot be redirected to an
unconfigured origin, repository grants remain effective at the data boundary,
and uncertain retries cannot create a second delivery or drift to another
commit.

This is repository selection, not provider-account discovery. GitHub App/
GitLab OAuth installation flows, encrypted per-installation credentials,
bulk selection, auto-enable-new-repository policy, enable/disable/delete
mutations, background clone orchestration, and repository lifecycle UI remain.
