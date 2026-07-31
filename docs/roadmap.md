# Diffuse delivery roadmap

The phases are ordered by dependency and risk, not by demo appeal. The full
capability set in `docs/capabilities.md` remains the goal throughout; each phase
must leave a deployable, observable system.

**No phase is complete until its exit condition passes in CI.** Not
demonstrated once by hand, not argued for in a pull request description, not
asserted in this document: a command in `.github/workflows/verify.yml` that
fails the build when the condition stops holding. A phase whose exit condition
has no such command is unfinished no matter how much of its body is built, and
this document says so in that phase's own text.

No phase's exit condition is enforced in CI today. `verify.yml` runs lint, unit
tests, PostgreSQL integration tests, packaged-image validation, and a stack
boot; it does not call `scripts/eval.sh`, and the two gates the rest of the
roadmap is measured against — review quality in Phase 0 and retrieval recall in
Phase 1 — have never produced a number. That ordering failure is the reason
Phases 2, 3 and 4 are largely built against constants nobody has measured:
every confidence threshold, severity floor, finding cap, and retrieval fusion
weight downstream of Phase 1 is a guess that no run has confirmed or refuted.
Work the Phase 0 and Phase 1 gates before work that depends on them.

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
  `service/eval_harness.py` now runs the review engine against committed
  fixtures and emits that `observed` half directly, so it no longer has to be
  transcribed by hand; `evals/fixtures/` holds eight labeled cases and
  `scripts/eval.sh` runs them and compares the scored result against a baseline.
  **The gate is not live yet: no baseline is committed.** Capturing one requires
  live model calls, so `scripts/eval.sh` exits non-zero with instructions rather
  than passing vacuously — see `evals/CAPTURE.md`. Until a baseline exists,
  every confidence threshold, severity floor, and finding cap remains an
  unmeasured constant. Remaining work: capture the baselines, add reviewed
  private PR fixtures alongside the synthetic ones, and establish release gates.
  `evals/` is packaged into the image and verified in CI, but the documented
  relative path does not resolve there — the in-image copy of the example suite
  is `/opt/diffuse/_internal/evals/baseline.example.json`.
- That gate is one funded run away. Everything it needs except the baseline is
  committed and working: `service/eval_harness.py run` drives the real engine
  over `evals/fixtures/`, `capture` and `check` are pure functions of a suite
  file so the regression logic is unit-tested without a credential,
  `scripts/eval.sh` refuses to pass rather than pass vacuously, and
  `evals/CAPTURE.md` is the exact procedure. What is missing is a credential
  and the decision to spend on it: 40 model calls, roughly $0.75 at an example
  $3/$15 rate card with no depth requested, under $0.10 on a small model
  (`evals/CAPTURE.md` §4). **The blocker is a spend decision, not
  engineering.** Three steps make the gate live: capture
  `evals/baselines/review-baseline.json`; prove it fails against a seeded
  regression, per `evals/CAPTURE.md` §6, because a baseline that has never
  failed is not yet known to be a gate; and call `scripts/eval.sh` from
  `.github/workflows/verify.yml`, which does not call it today.
- Be exact about what that gate covers, because its name invites overreading.
  It measures the review engine and nothing else. It does **not** measure
  retrieval: the harness reads every context entry verbatim from the fixture's
  `FixtureContext` list and never calls the retriever. That is deliberate — a
  baseline that depended on the state of an index snapshot would drift for
  reasons unrelated to review quality — but the consequence is that a retrieval
  regression is invisible to it, and the embedding removal in ADR 0043 shipped
  with that stated explicitly. It does **not** measure policy: the harness
  calls `generate_review` without a `policy` argument, so the engine runs with
  `policy=None` and exercises no repository rule, no path scope, no
  path-scoped confidence or severity floor, and no approved learned rule.
  Retrieval recall is Phase 1's gate and needs different machinery. Policy has
  no gate at all yet, and no phase currently claims one.
- Review, retrieval, and security evaluation is phase work, not a continuous
  workstream. It was listed as one until now, which is part of why it never
  started: an always-running workstream is never late, and nobody is behind on
  it. The review gate is this phase, the retrieval gate is Phase 1, and the
  security eval fixtures and noise gates are Phase 2's classified-security
  bullet. Each blocks the exit condition of the phase that owns it.
- Configuration validation is in place for the worker: `validate_worker_configuration`
  resolves every hot-path variable at startup and names the one that fails.
  Extend the same treatment to the API process, which still reads its
  configuration ad hoc.

Exit: every planned capability has an owner component, data boundary, and
testable acceptance condition.

Not met. The acceptance conditions are written down in `docs/capabilities.md`,
but written down is not testable: no command evaluates any of them. The command
that would evaluate the most consequential one is `scripts/eval.sh`, and it
needs the baseline.

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
- The hybrid retrieval foundation now combines graph with weighted
  path/symbol/content full-text search through deterministic rank fusion with
  snapshot provenance. The vector leg was removed in
  [ADR 0043](adr/0043-graph-and-lexical-retrieval-without-embeddings.md).
  Continue with richer lexical parsing, multi-hop traversal, summaries, and
  measured recall gates.
- The cross-repository foundation now resolves cascading explicit repositories
  and operator-managed same-host clusters into immutable, bounded snapshot
  plans, preserves repository-qualified retrieval provenance, and stores the
  exact related commits on review runs. Add tenant/RBAC authorization,
  dashboard management, cross-repository graph contracts, and recall evals.

Exit: evals demonstrate that changed functions retrieve their important
callers, dependencies, tests, and cross-repo contracts at a defined recall
target.

Never met, and not currently measurable. No retrieval eval exists. There is no
defined recall target, no labeled must-retrieve set for any fixture, and no
run that has produced a recall number for retrieval. The one harness that
exists, `service/eval_harness.py`, cannot serve here: it reads each context
entry verbatim from the fixture's `FixtureContext` list and never calls
`retriever.retrieve`, so it would score the fixture author rather than the
retriever. Meeting this gate means extending the harness to build a real index
over a real repository, call the retriever against it, and score the returned
set against labeled expectations per changed function — a separate and larger
piece of work than the Phase 0 review gate, which needs only a credential.

This is the dependency the phase ordering was supposed to respect and did not.
Phases 2, 3 and 4 all consume retrieved context and were built on top of an
unmeasured retrieval layer; the removal of the vector leg in
[ADR 0043](adr/0043-graph-and-lexical-retrieval-without-embeddings.md) was
accepted on reasoning alone for exactly the same reason. Nothing that consumes
retrieved context can be tuned honestly until this gate passes.

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
  retrieves graph/lexical context, validates cited ranges, respects
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

Not demonstrated, and this phase is built anyway. Onboarding, index currency,
and idempotency are exercised by the integration tests in CI. "High-signal" is
not exercised by anything: it is precisely what the Phase 0 gate measures, that
gate is not live, and so the deterministic 0–5 confidence scale, the
`MIN_REVIEW_CONFIDENCE` floor, the severity thresholds, the finding caps, and
the verifier pass in this phase's engine are all unmeasured constants. The
classified-security bullet above says its own version of this: the security
recall and noise gates it lists as remaining work are the security half of the
Phase 0 gate and have the same status.

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

Met except on "measure", which is the word that mattered. Inspect, version,
approve, and reverse are implemented and covered by tests; every applied
learned-rule version is snapshotted on its review run, so past output is
attributable. Nothing measures the difference any of it makes. The harness runs
`generate_review` with `policy=None`, so no repository rule, scoped override,
strictness setting, or approved learned rule is exercised by any evaluation. A
rule can be proposed, approved, applied, and snapshotted without a single
number showing that it improved review output or that reversing it would hurt.
A policy eval — the same fixtures scored with and without a given rule set — is
the missing gate, and it does not exist in any phase.

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
  `search_code` now fuses lexical and graph evidence, while separately scoped
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

Availability is broadly true and largely checked; quality is not checked at
all. The CLI, MCP, REST, and agent-handoff surfaces exist and are tested, but
the last bullet of this phase still lists retrieval and answer evaluations as
outstanding, and those are the Phase 1 gate under another name. `search_code`
and `ask_codebase` ship with no recall or grounding number, so a change to
fusion weights or traversal depth can degrade every one of these surfaces
without a single test turning red. Learned-rule moderation is available here
via `diffuse learning`, but see the open question below: available is not the
same as used.

## Phase 5 — Runtime validation

**Pending an explicit keep, defer, or delete decision. No work in this phase
starts until that decision is recorded.** The content below is unchanged and
stays in place; this is a flag, not a deletion, and the decision has not been
made.

The argument for deleting it. This phase generates code with a model and
executes it, and the code under test arrives from an untrusted pull request
opened by anyone who can open one. That makes container escape a review-path
vulnerability: the blast radius of a sandbox bug is the control plane, its
database, and the installation's SCM credentials — the exact asset the exit
condition below promises to protect, defended by the hardest boundary in
systems software. It is also the most expensive item on this roadmap, in
engineering to build and in per-review compute to run. And it is proposed by a
project whose review-quality gate is not live: there is no measurement showing
that static review is good enough to be worth extending, no measurement of what
runtime evidence would add on top of it, and no basis for preferring it to a
cheaper change to the review engine. Building the most dangerous and most
expensive feature before the cheapest measurement is the same ordering mistake
the top of this document describes.

The cost of keeping it. Sandbox backends and their hardening, stack and
build/test detection, test generation, budget enforcement, evidence capture,
review integration, and a standing security evaluation of the sandbox itself —
plus the permanent obligation to patch a remote code execution surface that
ships to self-hosted operators who did not build it and cannot audit it. That
obligation does not end when the phase does.

Deciding to defer is a valid outcome and should be recorded as one, with the
condition that would reopen it — most plausibly the Phase 0 and Phase 1 gates
being live and showing a class of defect that static review demonstrably
misses.

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
- Make the repository and both GHCR packages public after ADR 0042 lands, then
  extend the multi-architecture image pipeline and digest-pinned Compose bundle
  with Helm/Kubernetes and air-gapped profiles. The release pipeline now fails
  before creating a GitHub Release unless the image and bundle are anonymously
  pullable. The current final image packages one non-root executable without
  plain Python source or build-only material.
- Extend the existing keyless image signing, provenance, and SBOM attestations
  with offline update bundles. Per
  [ADR 0042](adr/0042-source-available-self-hosted-distribution.md) there is no
  entitlement or license-enforcement mechanism to build.
- Add backups, restore drills, migration rollback, opt-in telemetry controls,
  structured observability, and safe data export.

Exit: small and large self-hosted installations have documented SLOs,
recoverability, upgrade paths, signed distribution, and security controls
without a required Diffuse-hosted control plane.

Phase 6 is the last phase. Diffuse is self-hosted only, and there is no managed
cloud service on the roadmap — see
[ADR 0042](adr/0042-source-available-self-hosted-distribution.md).

## Open questions

These are product decisions, not engineering tasks. Each blocks work that is
already on the roadmap, and each stays here until an owner decides and an ADR
records it.

**Who works the learned-rule approval queue?**
[ADR 0013](adr/0013-evidence-bound-human-approved-learned-rules.md) makes every
suggested rule inert until an authorized human approves it, and that is a
deliberate safety property worth keeping: a model cannot activate its own
inferred policy, no finding can be silently trained away, security/correctness/
critical protection is a hard floor, and every activation carries an actor and
an immutable lifecycle event. The gap is not the approval requirement. It is
that nothing tells anyone a queue exists. Generation is a scheduled
low-priority job that writes `suggested` rows; the only surface that shows them
is `diffuse learning list <repository_id>` on an operator's shell, and the
moderation UI is listed as remaining Phase 3 work that in practice cannot land
before the web control plane in Phase 6. No one is notified, mailed, paged, or
shown a count anywhere they already look. The predictable outcome is that
suggestions accumulate and no rule ever reaches `active`, which means the
review behavior that is supposed to grow with a team never changes — the
central product differentiator, built, tested, and in practice unreachable.
Three options:

- Keep approval manual and add a notification surface: a pending count in the
  published PR footer, a scheduled digest, or a check-run annotation when a
  repository has suggestions waiting. Cheapest, preserves ADR 0013 unchanged,
  and still fails if the operator ignores it.
- Auto-activate above a confidence floor, with security, correctness, and
  critical-protected categories still requiring explicit approval. Rules start
  affecting reviews without a human in the loop, which contradicts ADR 0013's
  central decision and needs it superseded, not amended.
- Auto-activate in shadow mode: the rule applies, every finding it affected is
  labeled in the output as produced under an unconfirmed rule, and a human
  confirms or reverts retroactively from that evidence. Keeps the human, moves
  them after the fact, and gives them concrete output to judge instead of an
  abstract proposal. Most expensive of the three: it needs per-finding rule
  attribution in published output and a retroactive revert that restores
  suppressed findings.

The first leaves ADR 0013 intact and is a Phase 3 delivery item; the second and
third change its central decision and need an amending or superseding ADR
before any code. Doing nothing is also a choice, and the honest way to record
it is to say in `docs/capabilities.md` that learned rules do not activate in
practice, rather than leave them listed as a working foundation.

**Keep, defer, or delete Phase 5?** Stated in full under Phase 5 above.

## Continuous workstreams

Evaluation and regression gates were listed here. They are not continuous work
and listing them as such is part of why none of them started: a workstream that
is permanently in progress is never late and never blocks anything. They are
the exit conditions of the phases that own them — review quality in Phase 0,
retrieval recall in Phase 1, security recall and noise in Phase 2 — and they
now sit in those phases. What remains below is genuinely continuous.

- Language coverage and parser accuracy.
- Dependency, container, and sandbox security.
- Cost/latency optimization and model portability.
- Accessibility, internationalization readiness, and operator documentation.
- Public API stability and backwards-compatible migrations.
