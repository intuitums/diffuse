# ADR 0003: Credential-safe repository mirrors

- Status: accepted
- Date: 2026-07-23

## Context

Manual indexing from an operator-prepared checkout cannot keep a self-hosted
review service current. Putting access tokens in HTTPS clone URLs or Git command
arguments would expose them through database rows, logs, process listings, and
error messages. Mutable shared checkouts also cannot prove which commit an
indexer read.

## Decision

Diffuse registers repositories explicitly by provider, normalized host,
namespace/name, and default branch.

- HTTPS clone URLs are derived from validated repository identity and never
  contain credentials.
- Provider tokens remain environment-managed and are exposed to Git only
  through a non-interactive askpass helper.
- CLI and REST onboarding accept only the configured primary provider origin
  or an explicitly configured exact-origin allowlist entry.
- Credential helpers and interactive prompts are disabled for managed Git
  commands.
- Each repository has an ID-addressed bare mirror in a private operator-owned
  root. Existing symbolic links, non-bare repositories, and unexpected origin
  URLs are rejected.
- Initial clones are created in a same-filesystem temporary directory and
  renamed into place atomically.
- Fetch and worktree operations take a repository lock.
- Indexing uses a detached disposable worktree at a verified full commit SHA.
- Signed default-branch push deliveries advance a timestamped repository ref
  and enqueue an idempotent exact-commit job.
- Jobs for one repository ref execute serially; delayed older pushes cannot
  replace a newer recorded ref.

## Consequences

Operators no longer need to prepare checkouts or run the indexer manually, and
worker restarts do not lose repository state. Source remains inside the
self-hosted repository volume and immutable index snapshots remain tied to a
verified Git commit.

The initial credential source is an environment/secret-manager-projected token.
GitHub App and GitLab OAuth installation flows, encrypted per-installation
credential records, repository deletion, and multi-node mirror coordination
remain required for full production parity.
