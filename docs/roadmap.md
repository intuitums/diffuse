# Diffuse delivery roadmap

> **Historical delivery ledger — not the v1 product specification.** Public
> MCP, REST, service tokens, auto-approval, and agent handoffs described in
> present tense below are **removed from v1** (see [v1-scope.md](v1-scope.md)).
> Prefer that document for active scope; treat this file as phase history and
> unfinished measurement work.

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
  regression is invisible to it, and the embedding removal shipped with that
  stated explicitly. It does **not** measure policy: the harness calls
  `generate_review` without a `policy` argument, so the engine runs with
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
  snapshot provenance. The vector leg has been removed. Continue with richer
  lexical parsing, multi-hop traversal, summaries, and measured recall gates.
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
unmeasured retrieval layer; the removal of the vector leg was accepted on
reasoning alone for exactly the same reason. Nothing that consumes retrieved
context can be tuned honestly until this gate passes.

## Phase 2 — Review engine and SCM experience

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
  ~~Revision-safe MCP fix-one/fix-all actions~~ are removed from v1 published
  review output (suggested-fix text may remain). GitHub publication can also
  replace one human-preserving managed
  PR-description region, suppress top-level summary comments, recover retries
  idempotently, and ignore the resulting self-description webhook. Add manual
  diagram requests, commit-message previews, and richer history views.
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
  authentication, optional job submission to the operator's self-hosted worker,
  partial-stage continuation, and shell completion. Local agent-CLI review
  runtimes are tracked in [agent-runtimes.md](agent-runtimes.md) and
  [engineering-plan.md](engineering-plan.md), not as a deletion of the server.
- ~~The MCP foundation~~ is removed from v1. Historical notes about
  repository-scoped MCP tools, service tokens, `ask_codebase`, analytics
  projections, and agent handoffs belong to the pre-v1 surface; do not treat
  them as live. Internal `search_code` remains available to review runtimes.
- ~~The versioned REST foundation~~ (`/api/v1`) is removed from v1. Service
  tokens and public idempotency keys are not a live auth boundary.
- The agent-handoff foundation is removed from v1; Diffuse does not emit
  fix-one/fix-all MCP bundles.
- Extend source-linked search with multi-hop/type-aware traversal and
  retrieval evaluations (Q&A / `ask_codebase` is out of v1).

Exit: every review and customization workflow is available without the web UI.

Availability of the remaining CLI/operator workflows is broadly true and
largely checked; quality is not checked at all. `search_code` ships with no
recall number, so a change to fusion weights or traversal depth can degrade
review retrieval without a single test turning red. Learned-rule moderation is
available here via `diffuse learning`, but see the open question below:
available is not the same as used.

## Phase 5 — Source-execution validation

**Pending an explicit keep, defer, or delete decision. No work in this phase
starts until that decision is recorded.** The content below is unchanged and
stays in place; this is a flag, not a deletion, and the decision has not been
made.

This phase is **not** `REVIEW_RUNTIME` / agent-CLI review (renting Claude Code
or Codex for local `diffuse review`). It is generating tests and **executing
untrusted pull-request code** in a disposable sandbox. See
[agent-runtimes.md](agent-runtimes.md) for the former.

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
execution evidence would add on top of it, and no basis for preferring it to a
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

Exit: source-execution findings are isolated, reproducible, source-linked, and
cannot expose control-plane credentials.

## Phase 6 — Web control plane and enterprise operation

- Build onboarding, repository, settings, context/rules, review, analytics,
  audit, and operator experiences.
- Add organizations, teams, RBAC, OAuth/OIDC, SAML, interactive token
  administration/rotation, and optional SCIM.
- Add analytics UI, CSV/JSON exports, scheduled weekly reports, historical
  policy-eligibility and versioned cost inputs, usage limits, and governance
  policy.
- Ship supported Compose, Helm/Kubernetes, and air-gapped distributions.
- Make the repository and both GHCR packages public, then extend the
  multi-architecture image pipeline and digest-pinned Compose bundle with
  Helm/Kubernetes and air-gapped profiles. The release pipeline now fails
  before creating a GitHub Release unless the image and bundle are anonymously
  pullable. The current final image packages one non-root executable without
  plain Python source or build-only material.
- Extend the existing keyless image signing, provenance, and SBOM attestations
  with offline update bundles. There is no entitlement or license-enforcement
  mechanism to build.
- Add backups, restore drills, migration rollback, opt-in telemetry controls,
  structured observability, and safe data export.

Exit: small and large self-hosted installations have documented SLOs,
recoverability, upgrade paths, signed distribution, and security controls
without a required Diffuse-hosted control plane.

Phase 6 is the last phase. Diffuse is self-hosted only, and there is no managed
cloud service on the roadmap.

## Open questions

These are product decisions, not engineering tasks. Each blocks work that is
already on the roadmap, and each stays here until an owner decides.

**Who works the learned-rule approval queue?**
Every suggested rule is inert until an authorized human approves it, and that is
a deliberate safety property worth keeping: a model cannot activate its own
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
  repository has suggestions waiting. Cheapest, preserves the human-approval
  requirement unchanged, and still fails if the operator ignores it.
- Auto-activate above a confidence floor, with security, correctness, and
  critical-protected categories still requiring explicit approval. Rules start
  affecting reviews without a human in the loop, which contradicts the
  human-approval requirement outright rather than qualifying it.
- Auto-activate in shadow mode: the rule applies, every finding it affected is
  labeled in the output as produced under an unconfirmed rule, and a human
  confirms or reverts retroactively from that evidence. Keeps the human, moves
  them after the fact, and gives them concrete output to judge instead of an
  abstract proposal. Most expensive of the three: it needs per-finding rule
  attribution in published output and a retroactive revert that restores
  suppressed findings.

The first leaves the human-approval requirement intact and is a Phase 3 delivery
item; the second and third change it and need an explicit decision before any
code. Doing nothing is also a choice, and the honest way to record
it is to say in `docs/capabilities.md` that learned rules do not activate in
practice, rather than leave them listed as a working foundation.

**Keep, defer, or delete Phase 5 (source-execution validation)?** Stated in
full under Phase 5 above. Separate from shipping agent-CLI `REVIEW_RUNTIME`
adapters.

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
