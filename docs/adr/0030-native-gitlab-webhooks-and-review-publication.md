# ADR 0030: Native GitLab webhooks and review publication

## Status

Accepted

## Context

Diffuse could register and mirror GitLab repositories, but its webhook,
review-diff, publication, and status paths were GitHub-only. A provider label
in the repository table did not make GitLab reviews operational.

GitLab Cloud, Self-Managed, and Dedicated emit merge-request and push
webhooks. Newer versions can sign deliveries using Standard Webhooks headers;
older versions commonly use the shared `X-Gitlab-Token`. Merge-request
webhooks include the head commit but do not provide the merge-base required to
pin a review safely. GitLab's single-MR and diff-version APIs provide
`base_sha`, `head_sha`, and `start_sha`, although those fields can be empty
briefly after MR creation.

GitLab's target project owns the MR and its notes. For a fork MR, the source
project owns the branch commits and therefore the head-to-head comparison and
commit status. This distinction must survive the durable queue.

Relevant public contracts:

- <https://docs.gitlab.com/user/project/integrations/webhooks/>
- <https://docs.gitlab.com/user/project/integrations/webhook_events/>
- <https://docs.gitlab.com/api/merge_requests/>
- <https://docs.gitlab.com/api/repositories/>
- <https://docs.gitlab.com/api/notes/>
- <https://docs.gitlab.com/api/commits/>

## Decision

### Authentication and instance selection

- Add `/webhook/gitlab` for `Merge Request Hook` and `Push Hook` deliveries.
- Prefer Standard Webhooks authentication. Verify the base64 HMAC-SHA256
  signature over `<webhook-id>.<webhook-timestamp>.<raw-body>` using the
  decoded `whsec_` key, accept any valid `v1` signature during key rotation,
  and enforce a configurable 30–3,600 second replay window.
- Treat any partial or invalid Standard Webhooks envelope as a hard failure.
  Never downgrade it to legacy authentication.
- When no Standard Webhooks headers are present, compare
  `X-Gitlab-Token` in constant time and require a stable `Idempotency-Key` or
  `X-Gitlab-Event-UUID`.
- Resolve `X-Gitlab-Instance` only against the exact normalized primary origin
  or `GITLAB_ALLOWED_INSTANCES`. The signed payload cannot select an arbitrary
  API host.
- Scope deliveries by provider, exact SCM origin, and delivery ID, preserving
  coexistence with GitHub and other GitLab origins.

### Merge-request normalization

- Fetch current MR metadata before accepting review work.
- Use `diff_refs.base_sha/head_sha`; when those asynchronous fields are empty,
  inspect the latest matching diff version. Return a retryable 503 when GitLab
  has not prepared a safe diff identity instead of guessing a base revision.
- Preserve the target repository identity and the authoritative
  `source_project_id`. The latter is versioned in the provider-neutral event
  payload so fork comparisons and statuses use the source project.
- Map open, close, reopen, merge, code-push update, and review-relevant metadata
  update actions into the existing durable lifecycle and trigger model.
  Approval-only, reviewer-only, and other non-review actions do not start
  redundant model work.
- Treat GitLab's capped `1000+` change count as non-authoritative. The event is
  retained, but automatic trigger evaluation fails closed rather than applying
  an unsafe file-count policy decision.

### Push indexing and review execution

- Accept only non-deletion updates to the configured default branch.
- Fetch complete MR raw diffs from the target project.
- Fetch update comparisons between the previously reviewed and current heads
  from the source project. If GitLab reports a bounded/incomplete comparison,
  use the full MR diff conservatively for finding-continuity path evaluation.
- Dispatch review diff access and output by provider while retaining one native
  generation, policy, retrieval, lineage, and workflow implementation.

### Publication and status

- Publish the validated report as an idempotent GitLab MR note. Because this
  slice does not create line discussions, include every finding in the summary
  fallback and do not imply that an unavailable manual command exists.
- Pin note creation to the reviewed diff head and use a hidden
  review-run/head marker to recover the remote-create/local-commit crash
  window.
- When status output is enabled, create or update a GitLab external commit
  status on the source project and branch. Map Diffuse terminal conclusions to
  GitLab's `success`, `failed`, `canceled`, and `skipped` states, and retry the
  documented concurrent-update 409 response.
- Do not create finding-thread records when the provider published no inline
  finding comments.

## Consequences

GitLab Cloud and Self-Managed repositories can now keep their default-branch
index current and run the native Diffuse review workflow from MR webhooks,
including fork-safe diff comparison, durable lifecycle analytics, complete
summary publication, and optional commit status. GitHub and GitLab repositories
with the same namespace and delivery ID remain isolated.

Ingress makes one or two bounded GitLab API reads before acknowledging an MR.
This is required for safe identity but makes GitLab API availability part of
webhook acceptance. Metadata that is not prepared yet yields a retryable
response, and authentication/configuration failures are distinguished from
upstream API failures.

The process still uses one GitLab API token and webhook credential set.
Encrypted per-installation credentials remain required for production
multi-tenant onboarding. ADR 0031 subsequently adds exact-line discussions,
resolve/reopen operations, thread conversation, and feedback; ADR 0032 adds
manual triggers and automatic approval without changing this decision's ingress
and summary contracts.
