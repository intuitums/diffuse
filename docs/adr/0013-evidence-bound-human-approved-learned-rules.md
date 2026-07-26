# ADR 0013: Evidence-bound, human-approved learned rules

Date: 2026-07-23

Status: Accepted

## Context

Teams need suggested rules inferred from repeated team behavior after roughly
ten pull requests. Suggestions must be inspectable, modifiable, approvable, or
ignorable, and custom-context entries must expose evidence, scopes, and
`SUGGESTED`, `ACTIVE`, or `INACTIVE` state.

Feeding raw comments or reaction totals directly into review prompts would make
behavior opaque, non-reproducible, and vulnerable to prompt injection. A model
also cannot be allowed to activate its own inferred policy. Generation may race
new feedback, retries may repeat output, and a later rule edit must not rewrite
the provenance of an earlier review.

## Decision

- Maintain one durable learning scheduler state per repository. By default,
  generation starts only after at least ten effective feedback signals across
  ten distinct pull requests.
- Pin every low-priority generation job to the current effective-evidence
  fingerprint. A changed fingerprint makes the run `stale`; it is completed
  without model output and the repository is evaluated again.
- Present bounded feedback and finding context to a schema-constrained model as
  untrusted evidence. Each candidate must cite configured minimum event and
  distinct-PR counts. Unknown or insufficient citations are discarded.
- Store candidates as inert `suggested` rules. Consolidate stable exact
  fingerprints and narrowly bounded near duplicates instead of creating
  repeated suggestions. A rejected suggestion remains rejected when later
  evidence is attached.
- Preserve linked feedback evidence and immutable lifecycle events for
  proposal, evidence addition, edit, approval, rejection, deactivation, and
  reactivation.
- Require optimistic version checks and an authorized actor for every
  moderation action. The initial self-hosted surface is an operator CLI that
  trusts authenticated shell access and records `OPERATOR` provenance.
- Only `active` rules enter review policy. Repository-authored rules take
  precedence on conflict. Learned rules cannot override system safety,
  exact-diff grounding, structured output, or the requirement for a concrete
  defect.
- Include active rule contents and versions in the effective policy
  fingerprint and store an immutable snapshot of every applied learned-rule
  version on its review run.
- Do not use feedback for automatic finding suppression in this phase.
  Security, correctness, and critical protection metadata remains a hard floor
  for that future work.

## Consequences

Teams can turn repeated review behavior into usable self-hosted policy without
allowing the model or raw comments to silently train production reviews.
Suggestions are inspectable, reversible, retry-safe, and attributable, while
past reviews retain their exact learned-policy context.

The current implementation learns only from Diffuse finding-thread feedback and
commit outcomes. Top-level human review comments, GitLab, organization/team
scope, verified web/API identities, semantic clustering beyond bounded
near-duplicate comparison, evaluation dashboards, and adaptive noise ranking
remain outstanding.
