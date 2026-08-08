# Diffuse target architecture

> **Historical reference — not the v1 product specification.** This document
> describes the broad pre-v1 system, including public MCP, REST, conversations,
> analytics, cross-repository context, and policy features that are being
> removed or deferred. Use [v1-scope.md](v1-scope.md) and
> [agent-runtimes.md](agent-runtimes.md) for active decisions. Do not extend a
> legacy surface merely because it is described below.

## Direction

Diffuse starts as a modular monolith with separately runnable workers. Service
boundaries are explicit from the beginning so large installations can scale
webhooks, indexing, review, sandbox, and API workloads independently without
forcing small installations to operate a distributed system.

The review **contract** is native to Diffuse — `ReviewReport`, policy, lineage,
evidence, conversation and learning, and observability share one coherent data
model. How a report is *produced* is a pluggable review runtime: today a
transitional one-shot model-API path (`litellm`); the destination is an
isolated agent-runner executing Claude Code or Codex under a short-lived
session capability. The worker never executes a CLI or mounts agent credentials
in that target architecture. Review generation and SCM publication are separate
durable stages. See [agent-runtimes.md](agent-runtimes.md).

PostgreSQL schema changes are also durable workflow boundaries. Version 1 is a
frozen packaged baseline; subsequent migrations are consecutive append-only
SQL files. A migrator holds one transaction-scoped advisory lock, applies all
pending versions atomically, and records immutable checksums plus operator
identity. A populated unversioned database requires an explicit adoption
command and must first satisfy every baseline table/column check.
The supported Compose profile gates API and worker startup on a successful
migration job.

## Logical components

```text
GitHub / CLI / MCP / Web app
                  |
             API gateway
        +---------+----------+
        |                    |
  webhook ingress       control-plane API
        |                    |
        +------ durable workflows ------+
                     |
        +------------+-------------+----------------+
        |            |             |                |
  repository      indexer      review runtime  source-execution
   manager     + summarizer   (API or agent     validator + sandbox
        |            |             CLI)                |
        +------ code intelligence -+
                     |
           PostgreSQL / cache / object store
                     |
              isolated agent-runner (CLI)
```

> **Target, with a controlled-pilot slice now present.** The web app,
> source-execution validator and its sandbox, cache, and object store are not
> implemented. LiteLLM remains the default review runtime; a self-hosted worker
> can now dispatch a signed, bounded native CLI session to the isolated matching
> runner. The sections below describe the remaining pilot and cutover work.

## Deployment and ownership boundary

Diffuse uses one PostgreSQL-backed data plane, self-hosted by the operator:

```text
Self-hosted
  Operator
        |
  Diffuse API + workers + web app
        |
  Operator PostgreSQL + repository storage
```

There is no Diffuse-hosted control plane and no managed service.
PostgreSQL remains authoritative for repository, source-derived, review,
workflow, feedback, and learning state. Self-hosted telemetry and diagnostic
upload are opt-in.

Releases are delivered as signed, digest-pinned artifacts. Runtime images omit
the build-only material and compile the Python application rather than shipping
plain source files — a packaging and image-size decision, not a secrecy one:
the source is published under BSL 1.1.

### Control plane

> **Target, not current state.** Only the versioned REST API and scoped
> service-token authentication exist. There is no web UI, no organization,
> team, or role model, and no OIDC, SAML, or SCIM implementation — none of
> those three appears anywhere in the source.

- The v1 REST foundation exposes repository/index state, PRs, reviews,
  findings, analytics, and code search/Q&A plus idempotent repository
  onboarding, reindexing, and review requests. A web UI, settings, identity,
  and broader administrative mutations remain.
- Scoped service tokens and the bootstrap credential are the authenticated
  principals. The GitHub OAuth browser endpoints are mounted but no
  authenticator consumes the session they mint, so OAuth grants no access yet;
  OIDC and SAML will be added.
- Organizations, teams, users, roles, repositories, integrations, policies,
  rules, model settings, audit events, analytics, and operational state will be
  the control plane's resources. Repositories, policies, rules, model settings,
  and audit events are durable today; the tenancy model above them is not.

### SCM adapters

- One normalized interface for GitHub Cloud and GitHub Enterprise.
- SCM credentials are never placed in clone URLs, subprocess arguments,
  database rows, job payloads, or logs; they reach Git only through a
  non-interactive askpass environment. The self-hosted profile reads one
  process-level GitHub App identity, mints short-lived installation tokens, and
  keeps them only in memory. Per-tenant installation selection and encrypted
  database-backed credentials remain targets.
- Normalized repositories, commits, diffs, checks, reviews, inline threads,
  reactions, pull requests, and webhook events.
- Every event has a provider delivery ID and an idempotency record.
- The foundation verifies GitHub HMAC webhooks before a delivery is parsed or
  enqueued.

### Repository manager

- The initial implementation registers explicit GitHub HTTPS repositories and
  maintains ID-addressed bare mirrors in a private shared volume.
- CLI and REST onboarding allow only the configured primary SCM origin or an
  explicit exact-origin provider allowlist before askpass credentials can
  reach the host.
- Git credentials reach subprocesses only through a non-interactive askpass
  environment; clone URLs, arguments, database rows, and job payloads remain
  credential-free.
- Authenticated GitHub default-branch pushes enqueue exact commits. Workers
  fetch under a repository lock and index from ephemeral detached worktrees.
- Enforces the repository's enabled/onboarded state and, for token-authenticated
  callers, the token's repository claims before fetch or retrieval. There is no
  tenant boundary to enforce yet; see the cross-repository note below.
- Schedules initial indexing and push deltas. Deletion on revoked access and
  integrity repair are not scheduled: `workflow_jobs.job_type` admits exactly
  `review_pull_request`, `answer_review_comment`, `sync_review_feedback`,
  `generate_suggested_rules`, and `index_repository`.
- Emits immutable commit snapshots so a review and its citations are
  reproducible.

### Code-intelligence pipeline

1. Discover tracked files and repository instruction/context files.
2. Parse files with versioned language adapters. The foundation uses Python's
   native AST plus locally installed Tree-sitter wheels for JavaScript/JSX,
   TypeScript/TSX, Go, Java, Ruby, Rust, PHP, C, and C++; workers never download
   executable grammars at parse time.
3. Assign stable IDs to files, symbols, and relationships.
4. Store graph nodes/edges and symbol-aware code chunks.
5. Generate file/symbol/repository summaries.
6. Link imports, calls, inheritance, usage, tests, schemas, and cross-repo
   contracts.
7. Atomically activate the completed index snapshot.

An index snapshot records an index-format identifier derived from the adapter
schema, Python runtime, Tree-sitter runtime, and every installed grammar
version. Retrieval refuses a snapshot built by an incompatible format, while
unchanged chunks may still be copied forward when their exact boundaries and
content hashes match. The format also identifies the repository-policy schema.
Strict `.diffuse` layers, referenced context, and common instruction files are
written in the same activation transaction; snapshot readiness includes their
expected row counts and content fingerprint. Configured strictness becomes a
code-enforced post-verification severity floor. Description, summary-comment,
and fix-guidance preferences map losslessly; unsupported ignore semantics and
unknown fields fail indexing explicitly.

Retrieval combines:

- exact path/symbol and lexical search;
- graph expansion over callers, callees, dependencies, tests, and contracts;
- explicit configuration/context files;
- approved team memory and rules; and
- authorized cross-repository clusters.

Every result carries repository, commit, path, line range, retrieval reason,
score, and provenance.

The foundation stores a weighted PostgreSQL `tsvector` beside each immutable
chunk: paths and symbol names receive the highest weight, followed by content.
Review retrieval extracts only bounded code identifiers from changed lines,
excludes changed files from reference candidates, and uses weighted reciprocal
rank fusion across the graph and lexical channels. Combined channel provenance
is passed to the review engine with the pinned snapshot ID.

Cross-repository review context has two inputs: cascading, version-controlled
`context.repos` and operator-managed repository clusters. Every referenced
repository must already be onboarded and share the primary repository's SCM
provider and exact base host. Explicit entries are strict: disabled, missing,
or incompatibly indexed entries fail closed. Cluster-only members that are
temporarily disabled or not indexed are omitted. The union is deterministic,
deduplicated, and limited to seven related repositories.

Before a review run is created, the worker resolves each selected repository to
one compatible active snapshot and fingerprints the ordered plan. The review
stores every related repository ID/name, snapshot ID, commit, relationship
source, cluster IDs, and ordinal in `review_run_context_snapshots`. The
primary snapshot contributes graph and lexical candidates; related snapshots
contribute read-only lexical candidates. Fusion keys include repository
identity, formatted
context uses `owner/repo::path`, and the review verifier still permits findings
only on changed primary-repository lines. Cluster shell access is the current
operator authorization boundary; tenant/RBAC enforcement and cross-repository
graph edges remain future work.

Before retrieval, the worker resolves root-to-leaf policy for every changed
path from that pinned snapshot. Disabled and ignored files are removed before
the retrieval query is built. Review passes, confidence floors, custom
rules, and guidance remain path-scoped; summary-only mode is conservative
across the review. The review-run identity fingerprints the snapshot plus the
effective path policy, making retries and publications reproducible. A review
disabled for every changed path is stored as `skipped` and creates no SCM
publication.

Normalized pull-request events carry bounded author, target/source branch,
draft, label, title, description, authoritative changed-file count, and
trigger-origin metadata. Provider-reported creation, close, and merge
timestamps remain distinct from receipt and generic update time. Every
non-duplicate delivery that reaches an onboarded PR appends a
`pull_request_lifecycle_events` record, including stale deliveries that must
not replace current state. The current PR projection clears terminal
timestamps on reopen, preserves the authoritative creation timestamp, and
never invents a missing merge time from webhook receipt. This metadata is
part of both the workflow idempotency key and review context fingerprint, so a
ready transition, label change, or manual request at the same commit cannot
reuse a prior skipped decision. Automatic trigger policy is evaluated before
retrieval; denials are persisted with machine-readable reasons. Newer
same-revision metadata events supersede older queued decisions and running work
checks for newer jobs at every heartbeat.

Signed GitHub issue-comment events support deliberate `@diffuse` review
requests from human owners, members, and collaborators. The handler retrieves
fresh metadata for the open PR from the configured GitHub API and assigns the
comment ID as a unique manual trigger. Manual triggers bypass automatic
filters, while repository path disablement and review safety constraints still
apply.

Eligible reviews can opt into a `Diffuse code review` GitHub check through the
same cascading repository policy. The check is created against the normalized
head SHA before model work begins. Its PostgreSQL record and stable
`external_id` key recover both local retry and the remote-create crash window.
Verified findings at configured blocking severities produce failure; other
published reviews produce success. Superseded work is cancelled, exhausted
workflow retries fail, and only RIGHT-side changed lines become GitHub check
annotations. Check output neutralizes repository-authored mentions before
publication.

For a new head, the worker compares the last published review commit with the
normalized current head and records every touched old/new path. Verified
findings are deterministically matched to durable pull-request lineages using
exact fingerprints followed by bounded same-path/category text similarity.
Matched active findings remain open, matched addressed findings reopen, and an
unmatched active finding is addressed only when its file was touched.

Lineage events are written as provisional review evidence. They become active
in the same transaction that marks the commit-pinned review publication
durable; superseded and exhausted unpublished reviews discard them. Only new
lineages receive new inline comments. The provider root-note and thread
identities are stored, while addressed/reopened events create independently
retryable thread operations. Hidden operation markers recover reply crash
windows. GitHub GraphQL mutations resolve or reopen the bot-owned thread.
Status checks evaluate all active lineages rather than only findings emitted by
the latest model invocation.

Signed GitHub `pull_request_review_comment` events normalize explicit
`@diffuse` questions from authorized repository members. Ingress queues a
question only when its root comment belongs to a stored Diffuse finding thread
for that exact repository and pull request. Bots, Diffuse-authored markers,
acknowledgements, unrelated roots, comments without the mention, and `[Human
discussion only]` are ignored. Cascading path policy can disable
`respond_to_comments`.

Conversation jobs are pinned to the PR head carried by the signed event and
serialized by root thread. An older queued retry blocks later turns so history
cannot be reordered. The worker retrieves from one compatible immutable
snapshot using the question, finding, and exact file/line as a hybrid query;
the prompt also includes the original diff hunk and a bounded history of
published turns. Structured model references are retained only when their
exact range exists in the finding or retrieved chunks. Generated output,
usage, model, snapshot, and publication attempts are durable. A hidden
per-question marker recovers the provider reply-create crash window before the
same GitHub thread is called again.

### Review contract and runtimes

The review workflow is stateful. Steps 2 and 4 describe the target for the
one-shot API runtime; the parenthetical notes record what ships today. Agent-CLI
runtimes replace steps 3–5 with tool-driven investigation rather than a
pre-fused blob and pass fan-out — see [agent-runtimes.md](agent-runtimes.md).

1. Normalize PR metadata and diff into changed symbols and line ranges.
2. Resolve applicable organization/team/repository/directory policy.
   (Organization and team layers are planned; only the version-controlled
   `.diffuse/` repository and directory layers exist today.)
3. Build an impact set through graph traversal and hybrid retrieval.
   (One-shot API runtime: pre-fused context blob. Agent-CLI destination: the
   same retriever exposed as tools.)
4. Produce candidate findings. (Shipped API runtime: specialized passes —
   exactly `correctness`, `security`, `performance`, and `tests`. Agent-CLI
   destination: one investigation across concerns.)
5. Verify and deduplicate. (API runtime: independent verifier pass. Agent-CLI
   destination: separate session/subagent or cross-CLI provenance pair.)
6. Assign category, severity, confidence, evidence, and suggested fix.
7. Build the summary, risk score, issue table, optional diagrams, and status.
8. Publish/update SCM comments idempotently.
9. Track replies, reactions, subsequent commits, and addressed state.

Model output is parsed into a versioned schema. Raw model text is never posted
directly as an SCM action.

Steps 4 through 7 are a *review runtime*, selected by `REVIEW_RUNTIME`. The seam
is a whole `ReviewReport`, not a single model call: a runtime decides for itself
how many calls a review is. **Current state:** only `litellm` is selectable —
the one-shot API implementation above. The API and worker accept only that
value. **Destination:** `claude` and `codex` for `diffuse review` only,
driving a locally installed, locally authenticated agent CLI. Host plumbing for
Claude and Codex has landed (`diffuse agent login claude|codex` drives each
vendor's own auth into a Diffuse-owned config dir); no agent adapter is
selectable yet. A server has no developer CLI to drive, and the self-hosted
worker reviews pull requests from anyone who can open one, which is a different
threat model. `ReviewReport` is unchanged across runtimes, so the same
evaluation harness can score them on the same fixtures.

For a local agent-CLI review, it sits behind three independent boundaries,
because none of them covers the others: its environment is built from an
allowlist, so a credential Diffuse never names cannot reach it; the CLI's own
OS sandbox denies Bash egress and reads outside the worktree; and Diffuse
refuses to run below a version floor, because those sandbox settings are
version-gated and an older build drops the ones it does not recognize without
saying so. The sandbox's documented scope is Bash subprocesses — MCP servers
run outside it with full host privileges — so `--strict-mcp-config` is a
load-bearing control rather than defense in depth, and Diffuse's own MCP server
serves queries over an index and never executes repository-supplied content.
Native Windows is refused for agent-CLI runtimes (no OS sandbox).

The self-hosted server has a different, measured constraint: on the supported
Ubuntu/Docker profile Bubblewrap cannot create a user namespace even after the
tested AppArmor, seccomp, capability, no-new-privileges, and privileged-mode
variations. The server-side destination is therefore a separate review
compartment, not a weakened worker container and not an opt-in host-wide
user-namespace change. Before it can run an agent, that compartment must assert
at runtime that the untrusted-content process is non-root, has no control-plane
credential, cannot reach the database or other control-plane resources, and has
only the intended egress. This is a target boundary, not a selectable runtime;
the API and worker continue to accept only `litellm`. See
[SECURITY.md](../SECURITY.md) for the measured matrix and
[agent-runtimes.md](agent-runtimes.md) for the runtime split.

Before any review-model call, the worker fetches a bounded list of commit
metadata from the SCM and classifies authors, committers, verified bot
identities, and attribution trailers with deterministic rules. The classifier
never receives source and never invokes an LLM. High-confidence Anthropic or
OpenAI provenance makes the configured opposing family the candidate generator
and keeps the other configured model as the independent verifier; routing
permutes the configured pair and never contracts it onto a single model, so the
second opinion survives exactly where it matters most. Mixed or tool-only
attribution such as Cursor or Copilot retains cross-family candidate and
verifier models because the underlying generation model is not provable.

Only identities the SCM itself asserts—a bot login, or an agent email on a
commit whose signature the provider verified—can reach the routing threshold.
Git author names, author emails, and commit-message trailers are written by
whoever produced the commit, so they are recorded as evidence but capped below
it; otherwise the author of a change could choose which model reviews it.
GitHub returns the actor and verification state with the commit list. Missing,
stale, or forgeable metadata can only increase uncertainty: it never disables a
review or selects a weaker trigger policy. The evidence, confidence, model
plan, and routing reason are commit-pinned review-run state.

GitHub publication attaches eligible findings to exact diff lines and creates
Checks annotations. Publication can instead place the complete summary in one
reserved PR-description region while preserving text outside that region. The
mutation revalidates the open exact head and retries by replacement rather than
append; ingress ignores an edit only when stripping the managed region proves
that human-authored text is unchanged. Invalid or temporarily unavailable
positions fall back to complete finding details on an enabled summary surface.
Hidden review/finding markers and managed-region markers recover remote-create
crash windows. Top-level GitHub comments whose line starts with `@diffuse`
create distinct manual review events only after provider-native repository
authorization and current metadata enrichment.

#### Review readiness and footer identity

The published merge-readiness score is deterministic rather than another
unverified model claim. Diffuse maps independently verified 0–10 risk to a
0–5 confidence score, then only lowers the result for finding volume,
incomplete coverage, ignored paths, or zero reviewed files. Automatic approval
requires exactly 5/5 in addition to a zero-risk, zero-finding, fully covered
review.

The first publication attempt serializes on the durable pull-request row and
assigns the next positive review number. The number is stored on the review
run, protected by a per-pull-request uniqueness constraint, and reused by every
publication retry. GitHub output links the exact reviewed SHA and advertises
the already-authorized `@diffuse review` command; neither display state is
inferred from a count of eventually consistent remote comments.

#### Grounded change diagrams

Diagram generation is a separate structured stage and runs only when a
reviewable change crosses a deterministic complexity floor: at least 40
changed lines, or at least 12 changed lines spanning two files. The stage sees
bounded reviewable diff chunks and bounded retrieved context, treats both as
untrusted data, and can return no diagram. It selects one of sequence,
entity-relation, class, or flow based on relationships demonstrated by the
change.

Trusted validation requires the Mermaid directive to match the declared type
and enforces character, line, and line-width limits. Markdown fences, init
directives, click handlers, callbacks, URLs, HTML payloads, and Mermaid styling
directives are rejected. Only validated source is persisted and published.
Cascading path policy can disable diagrams; one disabled touched scope vetoes
the PR-level diagram. Collapse and default-open preferences are resolved into
the immutable review report so retry rendering cannot drift with later config.

#### Immutable output presentation

Summary, issues-table, confidence-score, and diagram presentation resolves for
every reviewable changed path. A section is included only when every touched
scope includes it, becomes collapsible when any scope requests collapse, and
starts open only when every scope requests default-open. Footer visibility
uses the same conservative all-scopes rule. These values are copied onto the
durable review report rather than re-reading repository configuration during
publication retries.

Description targeting, top-level summary visibility, and agent-fix visibility
use the same immutable report contract. If any reviewable scope requests a
managed description, that target wins for the PR. Any scope can
conservatively suppress the summary comment or published fix guidance.
Suppressing fix guidance never removes the durable suggested fix or MCP
finding data.

Presentation settings affect SCM display only. They never alter verified risk,
confidence, findings, status checks, automatic approval, lineage, or feedback
memory. If inline comments cannot be attached—or summary-only mode intentionally
suppresses them—validated finding details are rendered in the summary even
when the optional issues table is hidden.

#### Classified security review

The dedicated security pass uses an explicit trust-boundary threat model and
classifies every security finding:

- `vulnerability` means the current snapshot contains an attacker-controlled
  source, a reachable path, and a security-relevant sink or invariant break.
- `preventative` means the current snapshot is not exploitable, but a concrete
  future trust-boundary or caller change would make the changed code unsafe.

Preventative review is disabled by default and can be enabled per cascading
path with an independent confidence floor. The effective threshold is the
stricter of that floor and the ordinary review threshold. Preventative
findings are limited to medium or low severity; model output that labels one
critical or high is rejected rather than silently rewritten. Candidate
generation and independent verification both receive the classification
contract, while deterministic policy filters enforce it after model output.

Classification is part of candidate deduplication, durable fingerprints, and
finding-lineage matching. It is stored on findings and copied into feedback
and learning evidence so a later model cannot reinterpret a preventative
observation as a current vulnerability. GitHub comments, summary tables, and
check annotations visibly distinguish the two classes.

#### Conservative automatic approval

Automatic approval is a separate, default-off action after review publication;
a successful status check alone does not authorize it. Diffuse resolves
auto-approval policy for every old and new changed path. Every scope must
enable the action, the strictest risk ceiling and smallest file limit win,
exclusions accumulate, and each applicable inclusion set must match.

Eligibility is opt-in per path: a changed path must be named by an
`allow_paths` allowlist in every `.diffuse` scope that governs it, so a
repository that enables the action without allowlisting anything approves
nothing. The allowlist is resolved from the indexed default-branch snapshot,
never from the pull-request head, which is what keeps a pull request from
granting itself eligibility. The built-in critical-surface patterns are a floor
beneath it rather than the whole gate: an allowlist adds a required condition
and cannot remove one.

Eligibility requires authoritative metadata, a complete diff, full review
coverage with no ignored files, zero current findings and risk, and no active
finding lineage from an earlier commit. An explainable deterministic classifier
assigns inherent low, medium, high, or critical change risk separately from
defect severity. Critical surfaces—auth, public APIs, secrets, billing,
payments, schemas/migrations, CI, and infrastructure—can never be
auto-approved. Hard diff-size budgets also become critical rather than being
silently truncated.

Every requested decision is immutable and durable on the review run. The
publisher first verifies that the PR remains open, non-draft, and on the
reviewed head. Eligible GitHub publication uses an exact `commit_id` and hidden
idempotency marker. A changed head cancels the action without retrying stale
approval.

### Learning and memory

- Signed GitHub comment webhooks store authorized replies only when their root
  maps to a Diffuse finding in the exact repository and pull request.
- Workers periodically enqueue low-priority `sync_review_feedback` jobs. A sync
  lists GitHub reactions on the root finding note, verifies each actor is a
  GitHub collaborator, retains only 👍/👎, and appends observed/withdrawn
  transitions.
- Published addressed/reopened lineage transitions become commit-outcome
  signals in the same database transaction as review publication.
- Store source IDs, delivery hashes, actor authority, category/severity/security
  classification snapshots, and immutable signal transitions. Other emoji,
  bots, outsiders, unrelated roots, and `[Human discussion only]` replies do
  not train memory.
- Maintain inspectable repository-scoped signals with linked pull request,
  finding, category, severity, and path provenance.
- After a configurable evidence and distinct-PR threshold, enqueue low-priority
  `generate_suggested_rules` jobs against an exact evidence fingerprint. If
  evidence changes before generation, mark the run stale and reschedule rather
  than learning from a mixed snapshot.
- Require every generated suggestion to cite a minimum number of feedback
  events and pull requests. Consolidate exact and near-identical candidates,
  but keep generated suggestions separate from active rules.
- Record immutable proposal, evidence, edit, approval, rejection, deactivation,
  and reactivation events. Operator approval is the only transition that makes
  a suggestion affect review output.
- Merge active learned rules into the path-resolved policy beneath
  repository-authored rules, include their versions in the policy fingerprint,
  and snapshot every applied version on the review run.
- Apply hard policy floors so critical, security, and correctness categories
  cannot be suppressed by preference learning. The current foundation does not
  perform automatic noise suppression.

### Source-execution validator

> **Target, not current state.** No PR-code execution sandbox exists. Diffuse
> never executes pull-request code today: the review path reads source, queries
> the index, and calls model and SCM APIs (or, for local agent-CLI review, drives
> a sandboxed developer CLI that still must not execute untrusted repo content
> as Diffuse policy). This is distinct from `REVIEW_RUNTIME` agent-CLI review.
> `docs/capabilities.md` records source-execution validation as `planned`.

The source-execution validator will:

- create an isolated, short-lived sandbox from the reviewed commit;
- use repository-provided setup metadata and a constrained agent to generate
  targeted tests;
- execute with resource, network, filesystem, and time limits;
- redact secrets and treat repository code as untrusted; and
- store commands, generated tests, exit codes, logs, traces, screenshots, and
  recordings as immutable evidence objects.

Production backends may use Firecracker, Kubernetes Jobs with a hardened
runtime, or another policy-compliant sandbox. Running arbitrary PR code inside
the API or review container is prohibited.

### Developer surfaces

- The unified CLI manages repository onboarding/lifecycle, cross-repository
  clusters, learned-rule moderation, agent-CLI host sign-in, and local review.
  Local review uses the working-tree merge base, self-hosted active snapshot,
  cross-repository context, cascading policy, and approved learned rules. Today
  it deliberately still runs the API one-shot runtime; hosted CLI-native
  sessions are selected by the worker until local review adopts the same
  session contract. It emits human, inline-diff, versioned JSON, or
  terminal-safe agent text.
  A Git-common-dir state record permits failed/interrupted requests to restart
  only when every immutable input identity still matches.
- Complete the CLI with remote API authentication, optional remote job
  submission to the operator's self-hosted worker, partial-stage continuation,
  and shell completion.
- The MCP foundation is mounted at `/mcp` using stateless JSON Streamable HTTP,
  constant-time validation of an installation-wide recovery credential or a
  non-recoverable durable service token, explicit host allowlisting, and the
  same PostgreSQL source of truth as workers. Repository claims are enforced
  again in every PostgreSQL projection. Inspection tools project repositories,
  durable PR lifecycle state, review reports, current finding lineages/search,
  operator context, and feedback-derived context. A thin compatibility adapter
  resolves the public `name`/`remote`/`defaultBranch`/`remoteUrl` repository
  descriptor under the token's repository claims before querying by internal
  ID. Public camelCase parameters and PR/comment search aliases stay at the MCP
  boundary; storage and worker APIs remain Diffuse-native. Explicit write scope
  gates authoritative GitHub re-runs and audited context creation/update/
  deletion. Provider dispatch occurs only after repository-claim resolution,
  and each re-run re-fetches current provider state. Operator-context updates
  compare the caller's `expectedUpdatedAt` with the locked row, keep identical
  retries as no-ops, and record safe hashed deltas. Deletes preserve an audit
  tombstone and do not alter context snapshots already attached to review runs.
  Learned rules remain on their separate evidence-backed version/approval
  lifecycle.
- The REST foundation is mounted under `/api/v1` before the MCP catch-all and
  uses the same shared bearer authenticator and repository-scoped PostgreSQL
  projections. `diffuse:api:read` gates repository/index, PR, review, finding,
  analytics, and code-search reads. Model-backed Q&A additionally requires
  `diffuse:api:generate`. A manual review request requires read and write
  scopes, resolves the repository grant before provider dispatch, re-fetches
  the current open PR head, and enters the same audited durable queue as
  MCP. Required idempotency keys are stored only as actor/operation-scoped
  hashes; a request fingerprint detects conflicting reuse, a bounded lease
  coordinates concurrent attempts, and the normalized provider event is
  persisted before enqueue so a crash retry cannot drift. Missing and
  unauthorized objects share one 404 response, request bodies reject unknown
  fields, pagination is bounded, and failures use Problem Details. The
  bootstrap credential retains recovery access, while routine clients use
  hashed, expiring, revocable service tokens.
- REST repository creation requires administrative and all-repositories
  authority. It registers a non-conflicting enabled repository, verifies clone
  access through the locked credential-safe mirror, snapshots the resolved
  default-branch push event, and queues that exact commit. Reindexing requires
  read/write scope and the repository grant. Both mutations use longer bounded
  leases for clone/fetch, deterministic provider delivery IDs, the same
  transactional push-event queue as webhooks, exact response replay, and
  one audit event per accepted delivery.
- Source search resolves the caller-authorized repository descriptor to one
  compatible active index, freezes its exact snapshot plan, and then runs a
  first-class text query through lexical and graph-neighbor channels.
  Optional cluster context is intersected with the token's repository claims.
  Literal path prefixes constrain seeds and graph results. Every result includes
  repository-qualified lines, snapshot/commit identity, retrieval provenance,
  and an SCM-specific immutable commit permalink.
- Repository Q&A requires a separate generation scope. The model receives only
  bounded untrusted source excerpts and must return structured claims with exact
  repository/path/range citations. Diffuse retains a claim only when every
  citation maps unambiguously inside the supplied evidence; otherwise the whole
  claim is discarded. Zero grounded claims fails closed as insufficient
  evidence. Query/plan/source fingerprints and model/token provenance make the
  response inspectable even if a newer snapshot activates concurrently.
- Review analytics use the same repository-claim injection as every MCP read.
  The query accepts a required half-open UTC-normalized window of at most 366
  days and optionally resolves one public repository descriptor under those
  claims. Review attempts are counted once even when a run has many findings;
  applied findings are grouped separately by durable lineage. Current address,
  open-critical/security, reaction, and context-reply state is projected only
  for lineages selected by published runs in the window and is labeled with
  the database transaction's `asOf` time. An optional exact author filter is
  applied inside the same authorized SQL scope. Source-created PR cohorts
  provide reviewed/unreviewed counts; source-merged cohorts provide exact
  mean/median open-to-merge duration and UTC trends. The lifecycle ledger
  exposes merge-timestamp completeness, so missing provider data is visible
  rather than replaced with receipt time. The response defines every
  denominator and refuses to infer historical policy eligibility or monetary
  cost from missing versioned facts.
- Extend MCP with organization/team RBAC, generation rate/usage policy, report
  export/scheduling, and historical policy-eligibility/cost inputs.
- The agent-handoff foundation projects one current finding or all current
  findings from a published review into a revision-pinned, repository-scoped
  MCP bundle for Codex, Claude Code, Conductor, Cursor, Devin, and generic MCP
  clients. It refuses stale base/head revisions, closed PRs, addressed
  lineages, and superseded finding occurrences. GitHub review output names the
  exact handoff call. Add the optional local custom-URL bridge and per-user
  agent launch configuration for literal one-click buttons.
- Thread conversation and clarification in the SCM.

## Durable workflow model

The initial implementation uses PostgreSQL jobs and attempts. GitHub ingress
records a provider/host-scoped delivery and queues an exact PR or
default-branch revision in one transaction. Workers claim with `FOR UPDATE
SKIP LOCKED`, leases, bounded exponential retry, and terminal failed/dead
states.
Repeated deliveries and revisions are deduplicated, newer revisions supersede
older queued work, and jobs sharing one PR, review-thread, feedback-thread, or
ref scope cannot run concurrently. A newer commit both supersedes the queued
job and cancels work already running: the worker re-checks for a newer job at
every heartbeat, marks the review run `superseded`, cancels any pending
auto-approval, and concludes the GitHub check as `cancelled`.

The complete workflow contract still requires:

- per-tenant/repository concurrency and provider rate limits;
- resumable multi-stage indexing/review/sandbox workflows; and
- an operator UI/API for inspection and replay.

The PostgreSQL implementation can remain the small-installation backend. A
production profile may use a dedicated workflow engine as long as the domain
contract remains portable.

## Core data model

> **Target, not current state.** This is the intended full data model. Most of
> it is built: 33 of the 52 tables named below already exist, out of 45 tables
> created across `sql/schema.sql` and `sql/migrations/`. Those two are the only
> authoritative description of the current schema, and `diffuse database status`
> prints what is actually applied.
>
> The shipped schema also contains tables this section does not name:
> `code_symbols` and `code_relationships` (the snapshot-scoped graph),
> `repository_refs`, `pull_request_lifecycle_events`, `review_publications`,
> `scm_webhook_deliveries`, `scm_webhook_rejections`, `api_idempotency_keys`,
> `api_token_repositories`, `oauth_states`, `sessions`, and
> `user_installations`.
>
> Absent today: `organizations`, `teams`, `memberships`, `roles`,
> `scm_connections`, `repository_access`, `commits`, `index_jobs`, the separate
> `files`/`symbols`/`symbol_relationships` tables (graph data currently lives in
> snapshot-scoped tables), `rules`, `context_files`,
> `memory_signals`, `finding_resolutions`, `runtime_runs`, `runtime_artifacts`,
> and `usage_events`.

- `organizations`, `teams`, `users`, `memberships`, `roles`
- `scm_connections`, `repositories`, `repository_access`
- `commits`, `index_snapshots`, `index_jobs`
- `files`, `symbols`, `symbol_relationships`, `code_chunks`
- `repository_clusters`, `repository_cluster_members`
- `repository_policy_layers`, `repository_guidance_documents`
- `rules`, `context_files`, `memory_signals`, `learned_rules`,
  `custom_contexts`, `suggested_rule_evidence`, `learned_rule_events`
- `pull_requests`, `review_runs`, `review_check_runs`, `review_findings`,
  `finding_lineages`, `finding_lineage_events`, `finding_threads`,
  `finding_thread_operations`, `review_conversation_messages`,
  `review_feedback_sync_states`, `review_feedback_events`,
  `review_auto_approvals`,
  `suggested_rule_learning_states`, `suggested_rule_generation_runs`,
  `review_run_learned_rules`, `review_run_custom_contexts`,
  `review_run_context_snapshots`
- `finding_resolutions`
- `runtime_runs`, `runtime_artifacts`
- `workflow_jobs`, `workflow_attempts`
- `api_tokens`, `audit_events`, `usage_events`

The target tenant-owned rows carry an organization identifier. The initial
service-token tables are installation-scoped until the organization model is
introduced. Repository- and
commit-scoped records use foreign keys rather than a free-form `owner/repo`
string. Deletion is explicit and cascades through code, review history, and
artifacts according to configured retention policy.

## Deployment profiles

> **Target, not current state.** Only the Developer profile and a single-node
> Compose profile exist. The shipped Compose topology is four services — `db`,
> `migrate`, `app`, `worker` — as defined in `docker-compose.yml` (development)
> and `deploy/compose.yaml` (release, digest-pinned). Split index/review
> workers, a Redis-compatible cache, S3-compatible object storage, and the
> Kubernetes profile are not implemented; there is no Helm chart or Kubernetes
> manifest in the repository.

### Developer

- One API process, one worker, PostgreSQL.
- Local filesystem object storage.
- Local CLI authentication.

### Docker Compose

- Reverse proxy, web/API, webhook ingress, general worker, index worker,
  review worker, PostgreSQL, Redis-compatible cache, and
  S3-compatible object storage.
- Designed for a small team on one trusted Linux host.

### Kubernetes

- Independently autoscaled stateless services and workers.
- Managed PostgreSQL, Redis-compatible cache, and object storage.
- Dedicated sandbox node pool, network policies, pod security standards,
  external secrets, ingress TLS, and migration jobs.

### Air-gapped

- Mirrored, signed images and model artifacts.
- Local SCM endpoints and OpenAI-compatible inference endpoints.
- Offline install/upgrade bundle, SBOMs, migration plan, and rollback
  instructions.
- No license, telemetry, font, analytics, or asset dependency on the public
  internet.

## Security invariants

1. Verify webhook signatures before parsing or enqueueing.
2. Treat repositories, diffs, configuration, model output, and runtime output
   as untrusted input.
3. Enforce authorization in the data access layer, not only in handlers/UI.
4. Never place credentials or source text in routine logs.
5. Encrypt stored provider credentials with a rotatable key-encryption key.
6. Restrict SCM and model egress; defend URL fetches against SSRF.
7. Run PR code only in disposable sandbox isolation with no control-plane
   credentials.
8. Record administrative and review-mutating actions in the audit log.
9. Make repository revocation and deletion testable end-to-end.
10. Sign release images and publish dependency and container SBOMs.

## Evaluation gates

Each review-engine release must pass versioned evaluations for:

- changed-symbol and graph-edge extraction;
- caller/dependency/test retrieval recall;
- citation correctness;
- correctness/security finding recall;
- false-positive and nitpick rate;
- duplicate/conflicting findings;
- rule adherence and scope inheritance;
- feedback-learning behavior and protected-category floors;
- SCM idempotency; and
- sandbox escape and secret-exposure defenses.
