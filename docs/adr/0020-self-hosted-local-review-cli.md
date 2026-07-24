# ADR 0020: Self-hosted local review CLI

Date: 2026-07-23

Status: Accepted

## Context

Greptile publicly documents a terminal review workflow with a selectable base,
resume, inline diff, JSON, and agent-oriented output. Diffuse had separate
operator modules for repository and learning administration but no unified
developer command or local-branch review.

A local review that silently drops repository context would not be equivalent
to the SCM review engine. Automatically reading untracked files could also send
secrets or scratch data to a configured model without deliberate consent.

Public references:

- <https://www.greptile.com/cli>
- <https://www.greptile.com/changelog>

## Decision

- Package a `diffuse` entry point, mount existing repository, cluster, and
  learned-rule operator workflows beneath it, and implement `diffuse review`.
- Identify the enabled registered repository from `origin`, with explicit
  repository/SCM-host overrides for ambiguous self-hosted installations.
- Compare the current working tree with the merge base of `-b/--base`, or the
  registered default branch. Include committed, staged, and unstaged tracked
  changes. Report untracked files but require `--include-untracked` to read
  them.
- Require a compatible active immutable index. Resolve cascading checkout
  policy, approved learned rules, explicit/cluster cross-repository context,
  hybrid retrieval, and the same native generation/verifier path as SCM
  reviews.
- Support human output, `--diff`, versioned `--json`, terminal-safe `--agent`,
  and `--fail-on-findings`.
- Store only bounded review identity under Git's common directory. Resume a
  failed/interrupted request only when repository, diff, base, untracked
  choice, snapshot, policy, model, and prompt identities still match. Restart
  generation from its safe boundary; do not claim partial model-stage
  continuation.

## Consequences

Developers and coding agents can run context-rich reviews before opening a PR
without sending source through a hosted Diffuse control plane. Untracked input
is opt-in, terminal escape sequences are stripped, and a resumed request cannot
silently review different code or policy.

The foundation still needs remote API login and hosted execution, true
partial-stage continuation, shell completion, and distribution artifacts
beyond Python packaging and the self-hosted container image.
