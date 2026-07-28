# Architecture decision records

Why a subsystem is shaped the way it is. Read the relevant record before
changing indexing, retrieval, the review engine, publication, authorization, or
the database.

Records are append-only. A change that contradicts an accepted decision needs a
**new** record that supersedes the old one, and a note added to the old one's
`Status` line — not an edit to its Context or Decision.

Numbers are never reused, and a record is never deleted. ADR 0034 is absent
because the third-party configuration importer it described was removed
outright; the number stays retired.

Use [`TEMPLATE.md`](TEMPLATE.md) for a new record.

## Start here

These four carry the decisions the rest of the system is built on:

- [0001](0001-immutable-index-snapshots.md) — immutable commit-pinned index snapshots
- [0002](0002-postgres-workflow-queue.md) — PostgreSQL as the workflow queue
- [0036](0036-transactional-versioned-database-migrations.md) — transactional versioned migrations
- [0040](0040-proprietary-self-hosted-and-managed-cloud-distribution.md) — proprietary self-hosted and managed-cloud distribution

## All records

| # | Decision | Status |
|---|---|---|
| 0001 | [Immutable commit-pinned index snapshots](0001-immutable-index-snapshots.md) | Accepted |
| 0002 | [PostgreSQL-backed durable workflow queue](0002-postgres-workflow-queue.md) | Accepted |
| 0003 | [Credential-safe repository mirrors](0003-credential-safe-repository-mirrors.md) | Accepted |
| 0004 | [Native structured review generation and publication](0004-native-structured-review-engine.md) | Accepted |
| 0005 | [Versioned multi-language code graph adapters](0005-versioned-multilanguage-code-graph.md) | Accepted |
| 0006 | [Snapshot-pinned hybrid retrieval](0006-snapshot-pinned-hybrid-retrieval.md) | Accepted |
| 0007 | [Immutable cascading repository policy](0007-immutable-cascading-repository-policy.md) | Accepted |
| 0008 | [Metadata-aware review triggers](0008-metadata-aware-review-triggers.md) | Accepted |
| 0009 | [Durable GitHub status checks](0009-durable-github-status-checks.md) | Accepted |
| 0010 | [Durable finding lineage and review-thread state](0010-durable-finding-lineage-and-thread-state.md) | Accepted |
| 0011 | [Grounded review-thread conversations](0011-grounded-review-thread-conversations.md) | Accepted |
| 0012 | [Inspectable review-feedback memory](0012-inspectable-review-feedback-memory.md) | Accepted |
| 0013 | [Evidence-bound, human-approved learned rules](0013-evidence-bound-human-approved-learned-rules.md) | Accepted |
| 0014 | [Snapshot-pinned cross-repository context](0014-snapshot-pinned-cross-repository-context.md) | Accepted |
| 0015 | [Classified preventative security review](0015-classified-preventative-security-review.md) | Accepted |
| 0016 | [Conservative durable automatic approval](0016-conservative-durable-auto-approval.md) | Accepted |
| 0017 | [Deterministic review confidence and publication identity](0017-deterministic-review-confidence-and-identity.md) | Accepted |
| 0018 | [Grounded safe change diagrams](0018-grounded-safe-change-diagrams.md) | Accepted |
| 0019 | [Immutable review output presentation](0019-immutable-review-output-presentation.md) | Accepted |
| 0020 | [Self-hosted local review CLI](0020-self-hosted-local-review-cli.md) | Accepted |
| 0021 | [Authenticated read-only MCP foundation](0021-authenticated-read-only-mcp-foundation.md) | Accepted, amended |
| 0022 | [Scoped non-recoverable service tokens](0022-scoped-non-recoverable-service-tokens.md) | Accepted |
| 0023 | [Durable MCP pull-request lifecycle and custom context](0023-durable-mcp-pr-lifecycle-and-custom-context.md) | Accepted |
| 0024 | [Descriptor-addressed MCP contract boundary](0024-descriptor-addressed-mcp-contract.md) | Accepted, amended |
| 0025 | [Revision-safe agent fix handoffs](0025-revision-safe-agent-fix-handoffs.md) | Accepted |
| 0026 | [Immutable source search and grounded code Q&A](0026-immutable-source-search-and-grounded-code-qa.md) | Accepted |
| 0027 | [Audited custom-context mutation](0027-audited-custom-context-mutation.md) | Accepted |
| 0028 | [Repository-authorized review analytics](0028-repository-authorized-review-analytics.md) | Accepted |
| 0029 | [Authoritative pull-request lifecycle analytics](0029-authoritative-pull-request-lifecycle-analytics.md) | Accepted |
| 0030 | [Native GitLab webhooks and review publication](0030-native-gitlab-webhooks-and-review-publication.md) | Superseded by [0041](0041-github-only-source-control.md) |
| 0031 | [GitLab inline discussions and review interaction](0031-gitlab-inline-discussions-and-interaction.md) | Superseded by [0041](0041-github-only-source-control.md) |
| 0032 | [GitLab manual review and exact-head automatic approval](0032-gitlab-manual-review-and-auto-approval.md) | Superseded by [0041](0041-github-only-source-control.md) |
| 0033 | [Provider-neutral MCP manual review triggering](0033-provider-neutral-mcp-review-trigger.md) | Accepted, amended |
| 0035 | [Managed descriptions and immutable publication controls](0035-managed-description-and-publication-controls.md) | Accepted |
| 0036 | [Transactional versioned database migrations](0036-transactional-versioned-database-migrations.md) | Accepted |
| 0037 | [Repository-authorized versioned REST API](0037-repository-authorized-rest-api.md) | Accepted |
| 0038 | [Idempotent REST review trigger](0038-idempotent-rest-review-trigger.md) | Accepted |
| 0039 | [Idempotent repository onboarding and indexing API](0039-idempotent-repository-onboarding-and-indexing-api.md) | Accepted |
| 0040 | [Proprietary self-hosted and managed-cloud distribution](0040-proprietary-self-hosted-and-managed-cloud-distribution.md) | Accepted |
| 0041 | [GitHub as the only supported source-control provider](0041-github-only-source-control.md) | Accepted |

## Superseded records

- **0030, 0031, 0032** — the GitLab provider they specified was removed on
  2026-07-27. [0041](0041-github-only-source-control.md) records why, and which
  two GitLab-shaped defences were deliberately kept. The records stay for
  history; nothing in them binds the code.

## Amended records

Three records are still accepted but no longer describe the system exactly as
written. Their `Status` lines carry the detail:

- **0021** — its "read-only" constraint is void. ADR 0023 introduced
  write-scoped MCP tools and ADR 0027 added update and delete.
- **0024** — the legacy comment-search alias and its compatibility response
  field were removed from the server, and ADR 0041 voided the GitLab half of
  its tool-naming and `remoteUrl` decisions. The `merge_request` tool names it
  introduced still ship.
- **0033** — its provider neutrality now spans exactly one provider. The
  authoritative-refetch decision it records still binds.

## A note on this set

Most of these were written in a three-day burst alongside the initial
implementation rather than before it, so they read closer to feature
specifications than to decision records: there is little recorded disagreement,
and no record is marked Rejected. Treat them as accurate descriptions of *what*
and *why*, and be skeptical that the alternatives were seriously weighed.

New records should do better: state the alternatives actually considered and why
they lost.
