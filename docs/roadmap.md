# Diffuse delivery roadmap

The phases are ordered by dependency and risk, not by demo appeal. The full
capability set in `docs/capabilities.md` remains the goal throughout; each phase
must leave a deployable, observable system.

## Phase 0 — Product and platform contract

- Maintain `docs/capabilities.md` as the acceptance ledger.
- Adopt the target architecture and threat-model invariants.
- Architecture decisions and a transactional versioned PostgreSQL migration
  foundation are present. The frozen baseline and append-only catalog use
  immutable checksums, a transaction-scoped advisory lock, explicit verified
  adoption, status/verification commands, and a Compose startup gate. Add
  release-by-release downgrade policy, backup/restore drills, and migration
  compatibility CI across supported versions.
- A versioned review-evaluation *scorer* exists: given labeled and observed
  findings it computes true bugs, false positives/negatives, addressed
  findings, precision/recall/F1, median latency, tokens, and estimated cost.
  It does not yet run a review — `observed` is transcribed by hand into the
  input file, so nothing measures review quality automatically. Build a harness
  that invokes the review engine against fixtures, replace the committed
  synthetic example (whose recorded run has 0% recall) with reviewed private PR
  fixtures, package `evals/` so the documented command runs in the image, and
  establish release gates.
- Replace free-form environment access with validated configuration.

Exit: every planned capability has an owner component, data boundary, and
testable acceptance condition.

## Phase 1 — Repository and code-intelligence foundation

- Introduce repository, commit, index snapshot, file, symbol, relationship, and
  chunk identities.
- Secure GitHub installation onboarding and repository mirroring.
- The versioned language-adapter foundation now covers Python,
  TypeScript/JavaScript, Go, Java, Ruby, Rust, PHP, and C/C++, with
  parser-derived chunk boundaries and per-file failure isolation. Continue
  expanding grammar-specific usage/type resolution and evaluation fixtures.
- Build import/call/inheritance/usage/test edges and generated summaries.
- Implement atomic initial and incremental indexing.
- The hybrid retrieval foundation now combines graph, weighted
  path/symbol/content full-text search, and vector candidates through
  deterministic rank fusion with snapshot provenance. Continue with richer
  lexical parsing, multi-hop traversal, summaries, and measured recall gates.
- The cross-repository foundation now resolves cascading explicit repositories
  and operator-managed same-host clusters into immutable, bounded snapshot
  plans, preserves repository-qualified retrieval provenance, and stores the
  exact related commits on review runs. Add tenant/RBAC authorization,
  dashboard management, cross-repository graph contracts, and recall evals.

Exit: evals demonstrate that changed functions retrieve their important
callers, dependencies, tests, and cross-repo contracts at a defined recall
target.

## Phase 2 — Native review engine and SCM experience

- Native structured generation, verifier filtering, durable review history,
  summaries, deterministic 0–5 confidence plus 0–10 risk, severity/category,
  inline findings, suggested fixes, issue tables, durable review counters,
  exact-commit/re-trigger footers, and idempotent GitHub publication are
  implemented as the Phase 2 foundation.
- The diagram foundation auto-selects sequence, entity-relation, class, or flow
  output for non-trivial changes, rejects active/embedded Mermaid content,
  persists validated source, and honors cascading include/collapse/default-open
  controls.
- The output-presentation foundation snapshots cascading include/collapse/
  default-open controls for summary, issues, confidence, and diagrams plus
  footer visibility, while mandatory finding fallback prevents a display
  preference from losing review feedback on enabled summary surfaces.
  Revision-safe MCP fix-one/fix-all actions are now present in published review
  output. GitHub publication can also replace one human-preserving managed
  PR-description region, suppress top-level summary comments, hide agent-fix
  guidance, recover retries idempotently, and ignore the resulting
  self-description webhook. Add manual diagram requests, commit-message
  previews, custom-URL bridge buttons, and richer history views.
- The trigger-policy foundation now supports default draft/update behavior,
  include/exclude label/author/target-branch/keyword filters, file-change
  limits, same-SHA metadata supersession, durable skip reasons, and authorized
  GitHub `@diffuse` manual reruns.
- The status-check foundation now supports repository-scoped enablement,
  configurable blocking severities, exact-commit GitHub checks and annotations,
  durable retry recovery, supersession cancellation, and terminal failure.
- The finding-continuity foundation now matches finding lineages across commit
  updates, detects addressed and reopened findings from exact compare diffs,
  preserves active history, avoids duplicate inline comments, and
  idempotently resolves/reopens GitHub threads.
- The review-conversation foundation now accepts authorized explicit
  `@diffuse` questions on Diffuse-owned GitHub finding threads, verifies
  provider-native repository authority, keeps turns ordered and durable,
  retrieves graph/lexical/vector context, validates cited ranges, respects
  path-scoped disablement, and recovers reply crash windows without
  duplication.
- The classified-security foundation now separates presently exploitable
  vulnerabilities from opt-in preventative risks, applies path-scoped
  confidence and severity floors, persists the subtype through review memory,
  and labels GitHub review/check output. Add versioned security eval fixtures,
  measured recall/noise gates, dependency-aware analysis, and dashboard
  controls.
- The automatic-approval foundation now requires a clean fully covered review,
  no unresolved finding lineage, strictest-wins policy across both rename
  sides, independent low/medium/high/critical change-risk classification, hard
  critical-surface exclusions, exact-head revalidation, and durable idempotent
  GitHub approval. Add dashboard/org policy, audited actor credentials, risk
  evaluation fixtures, and operational approval analytics.
- Add dashboard-level trigger settings, focused manual review instructions,
  and provider-specific ready-for-review evaluation.
- Extend conversation to top-level/arbitrary-line questions.
- Complete encrypted per-installation credentials and provider-specific
  evaluation sets.

Exit: a self-hosted instance can onboard a repository, keep its index current,
and complete idempotent high-signal reviews.

## Phase 3 — Customization and team learning

- The repository-level foundation now provides immutable cascading
  `.diffuse/config.json`, `.diffuse/rules.md`, and `.diffuse/files.json`,
  stable scoped rules/overrides, referenced context, common instruction
  discovery, deterministic review controls, tracked custom context, and
  enforced strictness.
- Add dashboard-managed organization/team rules and context with authorization,
  audit history, and reset-to-inherited behavior.
- The inspectable-feedback foundation now stores authorized finding-thread
  replies, durably reconciles collaborator GitHub 👍/👎 reactions and
  withdrawals, projects addressed/reopened commit outcomes, exposes
  per-finding summaries, and marks security/correctness/critical signals as
  protected from suppression.
- The suggested-rule foundation now schedules evidence-fingerprinted generation
  after a configurable multi-PR history threshold, requires each proposal to
  cite supporting feedback, consolidates duplicates, exposes operator
  inspect/edit/approve/reject/deactivate/reactivate commands, applies only
  approved versions, and snapshots those versions on review runs.
- Extend collection to top-level human review comments,
  merged-but-unaddressed outcomes, and closed-PR polling retirement.
- Add preference ranking and noise suppression with protected
  security/correctness floors, then add organization/team moderation APIs and
  UI on top of the learned-rule lifecycle.
- Add external documentation/issue context and organization/team-managed
  cross-repository context.

Exit: teams can inspect, version, approve, reverse, and measure every behavior
that changes review output.

## Phase 4 — Developer surfaces

- The direct self-hosted CLI foundation packages a unified `diffuse` command
  for repository onboarding/lifecycle, cross-repository clusters, learned-rule
  moderation, and local review. Local review identifies an enabled checkout,
  compares its working tree to a selectable merge base, reuses immutable
  same/cross-repository context and approved learned rules, supports inline
  diff, JSON, agent text, opt-in untracked files, script exit gates, and
  unchanged-input resume after interruption/failure. Add remote API
  authentication, hosted execution, partial-stage continuation, and shell
  completion.
- The MCP foundation now serves repository-scoped inspection and write tools for
  repositories, PR lifecycle state, review reports, current finding lineages,
  finding/context search, and feedback-derived context over stateless JSON
  Streamable HTTP. Durable non-recoverable tokens add expiration/revocation,
  audited operator lifecycle, and fail-closed repository scopes while the
  environment credential remains a recovery path. Explicit write scope now
  gates authoritative GitHub re-runs and custom-context creation; active
  context is path-scoped, fingerprinted, and snapshotted on reviews. Public
  repository descriptors, camelCase inputs, both PR-list names,
  PR-comment projection, and the documented comment-search name now form a
  compatibility layer over repository-scoped durable state. Commit-pinned
  `search_code` now fuses lexical/vector/graph evidence, while separately scoped
  `ask_codebase` fails closed to claim-level citations inside the retrieved
  evidence. Both support literal path scope and optional token-authorized
  cluster context. Operator custom context now has write-scoped, compare-and-
  swap update/delete operations with safe audit deltas/tombstones; learned rules
  retain their separate approval lifecycle. A Diffuse-native analytics
  projection now aggregates repository-authorized half-open windows into exact
  review status/latency/token/custom-context/approval metrics, applied-finding
  and current-state rates, reaction/context engagement, repository and UTC
  daily trends, and linked open findings with explicit denominator and
  unavailable-metric definitions. Authoritative SCM creation/close/merge
  timestamps now flow through an append-only lifecycle ledger and current PR
  projection, adding author filtering, exact opened reviewed/unreviewed
  cohorts, mean/median merge time, timestamp-completeness rates, and PR/
  merge daily trends without receipt-time inference. Add organization/team
  RBAC, generation rate/usage policy, historical
  policy-eligibility facts, scheduled weekly reports, and CSV/JSON export.
- The versioned REST foundation exposes repository/index state, PRs, reviews,
  findings, analytics, hybrid code search, and grounded Q&A under `/api/v1`.
  It reuses repository-scoped service tokens, separates read from generation,
  bounds pagination and request bodies, hides unauthorized objects as missing,
  and publishes an OpenAPI contract. Its first write route provider-revalidates
  and audits manual review triggers, with hashed actor-scoped idempotency keys,
  request-conflict detection, leases, exact response replay, and crash-stable
  provider event snapshots. Admin/all-repository onboarding and
  repository-grant-scoped reindex requests now validate explicit SCM origins,
  resolve through credential-safe mirrors, persist exact push events, and reuse
  the webhook queue with the same durable replay/audit contract. Add
  enable/disable/delete and context/rule mutations, provider discovery,
  outbound webhooks, quotas, retention policy, and compatibility policy.
- The agent-handoff foundation emits exact-revision, current-lineage fix-one
  and fix-all bundles for Codex, Claude Code, Conductor, Cursor, Devin, and
  open MCP clients without granting checkout or push authority. Add the
  optional local bridge, custom URL registration, per-user agent selection,
  and direct launch UX.
- Extend source-linked search and Q&A with multi-hop/type-aware traversal,
  durable usage/rate accounting, a web UI, and retrieval/answer evaluations.

Exit: every review and customization workflow is available without the web UI.

## Phase 5 — Runtime validation

- Add hardened disposable sandbox backends.
- Detect repository stack and build/test workflow safely.
- Generate and execute targeted tests with strict budgets.
- Capture and attach reproducible evidence artifacts.
- Add runtime-validation policy, filters, review integration, and security
  evaluations.

Exit: runtime findings are isolated, reproducible, source-linked, and cannot
expose control-plane credentials.

## Phase 6 — Web control plane and enterprise operation

- Build onboarding, repository, settings, context/rules, review, analytics,
  audit, and operator experiences.
- Add organizations, teams, RBAC, OAuth/OIDC, SAML, interactive token
  administration/rotation, and optional SCIM.
- Add analytics UI, CSV/JSON exports, scheduled weekly reports, historical
  policy-eligibility and versioned cost inputs, usage limits, and governance
  policy.
- Ship supported Compose, Helm/Kubernetes, and air-gapped distributions.
- Extend the existing private multi-architecture image pipeline and
  digest-pinned Compose bundle with Helm/Kubernetes and air-gapped profiles.
  The current final image already packages one non-root executable without
  plain Python source or build-only material.
- Extend the existing keyless image signing, provenance, and SBOM attestations
  with connected update entitlement, offline signed entitlement files and
  update bundles, and documented commercial support windows.
- Add backups, restore drills, migration rollback, opt-in telemetry controls,
  structured observability, and safe data export independent of entitlement
  state.

Exit: small and large self-hosted installations have documented SLOs,
recoverability, upgrade paths, proprietary distribution, and security
controls without a required Diffuse-hosted control plane.

## Phase 7 — Managed cloud service

- Operate the same versioned API, worker, PostgreSQL/pgvector, repository
  storage, and migration artifacts used by self-hosted installations.
- Build cloud account, subscription, entitlement, provisioning, regional
  placement, deployment registry, support, and fleet-operation capabilities.
- Add a signed, replay-bounded data-plane status contract for rebuildable
  projections, with a durable outbox, per-deployment key rotation, version
  negotiation, and idempotent command handling.
- Keep source, diffs, embeddings, prompts, evidence, findings, workflow state,
  and learned rules in the managed data plane rather than the cloud
  control-plane database.
- Add tenant isolation, managed secret storage, regional backups, restore
  drills, capacity management, metering, billing reconciliation, SLOs, and
  incident-response procedures.

Exit: online customers can buy the same Diffuse engine as a managed service
without forking its database, review behavior, or release train.

## Continuous workstreams

- Review/retrieval/security evaluation and regression gates.
- Language coverage and parser accuracy.
- Dependency, container, and sandbox security.
- Cost/latency optimization and model portability.
- Accessibility, internationalization readiness, and operator documentation.
- Public API stability and backwards-compatible migrations.
