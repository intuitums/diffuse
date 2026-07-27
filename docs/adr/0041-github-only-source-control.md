# ADR 0041: GitHub as the only supported source-control provider

Date: 2026-07-27

Status: Accepted

Supersedes ADR 0030, ADR 0031, ADR 0032.

## Context

ADRs 0030, 0031, and 0032 built GitLab to parity across webhooks, review
publication, inline discussions, manual review, and automatic approval. That
parity was real, and it was expensive to hold.

Every provider-facing subsystem carried two implementations behind a `provider`
discriminator: seven `service/gitlab*.py` modules mirroring seven
`service/github*.py` modules, a second webhook route with its own signature
scheme (Standard Webhooks plus a legacy `X-Gitlab-Token` fallback plus an
instance allowlist), and ten `elif event.provider == "gitlab"` dispatch branches
in `service/worker.py` alone. Seven environment variables existed only to
configure the second provider.

The forcing decision is DEV-205: per-installation GitHub App tokens and the
multi-tenant schema that depends on them. Diffuse authenticates today with a
single global `GITHUB_TOKEN` refreshed on a timer, which is structurally
incompatible with multi-tenancy. Replacing it means threading installation
identity through every credential call site. Doing that work twice — once for
GitHub App installations and once for a GitLab token model that has no
equivalent concept — would roughly double the cost of the change that unblocks
everything else.

No customer is using the GitLab path. It was built ahead of demand.

## Alternatives considered

- **Keep GitLab and build tenancy for both.** Rejected on cost. GitLab has no
  installation primitive analogous to a GitHub App installation, so
  per-installation credential routing would need a parallel design, not a
  parallel implementation. This is the option that motivated the decision.
- **Keep GitLab frozen but present — no tenancy, single global
  `GITLAB_TOKEN`.** Rejected as worse than either alternative. It leaves a
  documented, advertised provider that silently cannot be used by any tenant
  but the first, and every future change still has to reason about both
  branches to avoid breaking the frozen one.
- **Extract a provider interface and keep GitLab behind it.** Rejected for now.
  The abstraction cost is real and the second implementation is what pays for
  it; with one provider the indirection is pure overhead. The event dataclasses
  in `service/scm.py` already provide the seam if a second provider returns.

## Decision

GitHub is the only supported source-control provider.

The seven `service/gitlab*.py` modules are deleted, along with their tests. The
`POST /webhook/gitlab` route is removed from `service/webhook_server.py`. The
five event dataclasses in `service/scm.py` accept `provider == "github"` only,
and every provider dispatch in `service/worker.py` raises `NonRetryableError`
for anything else. The `GITLAB_*` environment variables are removed from
`.env.example` and `deploy/env.example`. `McpRemote` and the REST
`RepositoryCreateRequest.remote` field narrow to `Literal["github"]`.

Two GitLab-shaped defences are deliberately **kept**, because they are
protections rather than support:

- the `glpat-` pattern in `service/review_failure_notice.py`, so a stale token
  in an operator's environment is still redacted from a failure notice;
- `.gitlab-ci.yml` in `CRITICAL_PATH_PATTERNS` in `service/auto_approval.py`, so
  that file stays ineligible for automatic approval — a GitHub-hosted repository
  can still contain one.

## Consequences

This makes the DEV-205 tenancy work single-implementation: one credential
model, one installation primitive, one webhook signature scheme.

It commits us to GitHub. Restoring GitLab means restoring deleted modules from
history and re-teaching every dispatch site about a second provider — cheaper
than the original build because `service/scm.py` keeps the provider-neutral
event shape, but not free.

Two migrations are deliberately **not** performed here, and each is a known
loose end:

- The nine `scm_provider IN ('github', 'gitlab')` CHECK constraints in
  `sql/schema.sql` are unchanged. Narrowing them would fail against any existing
  `'gitlab'` row, so it needs its own reviewed migration once production is
  confirmed clean. Until then `service/workflow.py` skips non-GitHub rows in
  `schedule_due_feedback_sync_jobs` rather than mis-routing them at a GitHub API
  base.
- The GitLab-shaped fields on the `service/scm.py` event dataclasses
  (`source_project_id`, `thread_id`, `start_sha`) are retained. `from_payload`
  accepts versioned key sets `metadata_v1` through `metadata_v5`, and queued
  jobs in the live database carry those keys; removing the fields would break
  deserialization of in-flight work across the deploy. They can go once the
  queue has drained.

No index snapshot is invalidated: neither `POLICY_SCHEMA_VERSION` nor
`LANGUAGE_ADAPTER_SCHEMA_VERSION` changes, so no `diffuse repository sync --all`
is required.
