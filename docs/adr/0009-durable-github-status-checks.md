# ADR 0009: Durable GitHub status checks

- Status: accepted
- Date: 2026-07-23

## Context

A code-review comment and a merge-gating signal have different contracts.
Repository operators need a stable check on the reviewed commit that can be
used by branch protection, while workers need to survive retries, process
crashes, duplicate webhooks, and a newer pull-request event without leaving an
ambiguous in-progress result.

GitHub's Checks API creates a check run for an explicit `head_sha`, supports an
operator-owned `external_id`, and accepts completed conclusions plus bounded
line annotations. Greptile's public repository configuration exposes an
optional status-check outcome. Diffuse needs the same operator result without
making a remote SCM object the source of truth for workflow state.

## Decision

- Cascading repository trigger policy adds `status_check`, defaulting to
  `false`, and `blocking_severities`, defaulting to `critical` and `high`.
- A pull request creates a check when at least one reviewable changed path opts
  in. Enabled path scopes combine their blocking severities conservatively.
- The worker creates the named `Diffuse code review` check against the exact
  normalized head commit before retrieval or model generation.
- Each native review run has at most one `review_check_runs` row. Its stable
  external key is derived from the immutable review-run ID. Creation,
  in-progress, completing, completed, and recoverable failure states are
  persisted independently from review-comment publication.
- Before creating a remote check, the adapter searches checks for the exact
  commit and external key. This closes the crash window in which GitHub accepted
  creation but the worker did not persist the returned ID.
- A completed report fails when any verified finding has a configured blocking
  severity and succeeds otherwise. A non-published report is skipped. A newer
  event cancels an existing in-progress check, and a terminal workflow failure
  completes it as failure.
- Verified RIGHT-side findings become annotations, capped at GitHub's per-update
  limit. LEFT-side findings remain in the review body because deleted lines
  cannot be valid head-commit annotations.
- Repository-authored text is length-bounded and `@` mentions are neutralized
  before it is sent to a check summary or annotation.
- Operators must supply a GitHub App installation or user access token with
  Checks write permission. Status-check publication remains disabled unless
  repository policy explicitly opts in.

## Consequences

Branch protection can require one deterministic Diffuse check on the exact
reviewed revision. Workflow retry does not create duplicate checks, a
remote-create crash is recoverable, and superseded work does not remain
indefinitely in progress. The review report remains the durable source for the
decision and annotations.

The foundation supports GitHub only. It does not yet update check annotations
across review threads, expose check policy in a dashboard, publish more than the
first bounded annotation set, or provide an equivalent GitLab commit status.
