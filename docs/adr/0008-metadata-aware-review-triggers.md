# ADR 0008: Metadata-aware review triggers

- Status: accepted
- Date: 2026-07-23

## Context

Pull-request eligibility cannot be represented by commit SHA alone. A PR may be
opened as a draft and later marked ready without changing code. Adding a
required label, removing a disabled label, editing a keyword, or deliberately
requesting another review can likewise change the correct decision at the same
base/head revision.

Evaluating filters in a model prompt would still pay retrieval/model cost,
could be ignored by the model, and would leave no reliable record of why a
review did not appear. Trusting arbitrary issue commenters to force model work
would also create a cost-abuse path.

Operators need draft/update controls, label/author/target-branch/keyword
filters, file-change limits, and manual PR comment triggers, and those outcomes
must be reachable entirely inside the self-hosted boundary.

## Decision

- Versioned repository policy includes cascading automatic, draft, update,
  label, disabled-label, included/excluded author, included/excluded target
  branch, included/excluded keyword, and file-change-limit settings.
- Filter patterns are bounded and case-insensitive. They support segment
  wildcards, recursive wildcards, single-character wildcards, and bounded brace
  alternatives. Brackets and leading exclamation marks remain literal.
- Each changed path receives root-to-leaf trigger settings. For PR-level
  evaluation, permissive booleans use OR, exclusion filters accumulate,
  inclusion filters are unrestricted when any applicable path is unrestricted,
  and the smallest file-change limit wins.
- GitHub PR events normalize bounded author, source/target branch, draft,
  labels, title, description, and authoritative changed-file-count metadata.
  Closed payload schemas prevent
  unreviewed fields or secrets from entering workflow jobs.
- The canonical metadata/trigger fingerprint participates in workflow and
  review identity. A newer event supersedes older queued work in the same PR
  scope even when base/head SHAs are unchanged. Heartbeats reject work when a
  newer queued or running decision exists.
- Automatic eligibility is evaluated before retrieval or embedding. Denials
  become `skipped` review runs with a stable `skip_reason`, zero model tokens,
  and no SCM publication.
- Signed GitHub `issue_comment` events accept commands whose line starts with
  `@diffuse` only from human owners, members, or collaborators. Fresh open-PR
  metadata is fetched from the configured GitHub API. The comment ID becomes
  the manual trigger identity.
- Manual triggers bypass automatic draft, update, and metadata filters. They do
  not bypass repository path disablement, exact-diff grounding, model-output
  validation, or publication safety.

## Consequences

Operators can predict and audit why a review ran or did not run, unwanted PRs
consume no inference budget, and ready/label/manual transitions cannot collide
with an earlier same-SHA skip. Manual reruns remain idempotent per comment while
allowing another comment to deliberately review the same revision again.

GitHub status checks are defined separately by ADR 0009, while explicit
questions on Diffuse-owned inline threads are defined by ADR 0011. The
  foundation does not yet expose trigger settings in a control-plane UI, treat
  instructions in top-level manual comments as focused questions, or authorize
  through organization/team policy. ADR 0016 adds eligible clean-change
  approval, and ADR 0032 extends comment triggers and approval to GitLab.
