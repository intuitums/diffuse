# Diffuse

Diffuse is an independently implemented, fully self-hostable code-intelligence
and review platform. Its product target is feature parity with Greptile:
graph-aware repository understanding, high-signal pull-request reviews,
learning from team feedback, cross-repository context, developer tools, runtime
validation, and enterprise-ready operations without requiring source code to
leave the operator's environment.

The current code is the first foundation, not the finished product. It indexes
tracked source files, combines graph and semantic retrieval, runs a native
structured review engine, and publishes commit-pinned GitHub reviews and status
checks plus GitLab exact-line discussions, merge-request summaries, and commit
statuses. Both providers support durable finding threads, grounded
clarification replies, inspectable feedback, authorized manual reruns, and
conservative exact-head automatic approval. The
feature-parity ledger distinguishes this working foundation from the remaining
product surface.

See:

- [Feature parity contract](docs/feature-parity.md)
- [Target architecture](docs/architecture.md)
- [Delivery roadmap](docs/roadmap.md)
- [Single-server deployment](docs/deployment.md)

## How it works

```text
Repository onboarding CLI or versioned REST API
  ├─ registers an explicit GitHub/GitLab repository
  ├─ derives a credential-free HTTPS clone URL
  ├─ maintains a locked bare mirror
  └─ queues the exact default-branch commit for indexing

GitHub or GitLab push / PR/MR / review-thread webhook and reaction ingestion
  └─ service.webhook_server
       ├─ verifies the webhook signature
       ├─ normalizes an exact commit or PR revision
       ├─ records authorized review replies as inspectable feedback
       └─ transactionally deduplicates and queues review/conversation work
            └─ service.worker
                 ├─ leases the durable job
                 ├─ fetches and indexes an exact commit in a disposable worktree
                 ├─ resolves exact primary + related-repository snapshots
                 ├─ retrieves graph + lexical + vector context with provenance
                 ├─ runs specialized structured review passes plus verification
                 ├─ reconciles authorized 👍/👎 reactions on finding comments
                 ├─ infers evidence-cited rule suggestions for human moderation
                 ├─ applies only approved learned-rule versions
                 └─ publishes provider-native reviews and status idempotently
```

The webhook acknowledges work only after its delivery and review job are
persisted. Workers use leases, bounded exponential retry, dead-letter state,
revision deduplication, and queued-job supersession.

## Deployment models

Diffuse is proprietary software with two planned operating models:

- **Self-hosted:** customers run the API, workers, PostgreSQL/pgvector,
  repository storage, and model connections in infrastructure they control.
  Standalone operation does not require a Diffuse-hosted control plane, and
  source-derived data does not leave the customer environment unless the
  operator explicitly configures an external model or integration.
- **Managed cloud:** Diffuse operates the same versioned data plane and
  PostgreSQL contract for the customer, with an additional cloud control plane
  for accounts, subscriptions, provisioning, deployment management, and
  support.

Self-hosting is a deployment right, not an open-source license grant. The
source repository remains private; customers receive authenticated, signed
executable artifacts and installation documentation under a commercial
agreement. See
[ADR 0040](docs/adr/0040-proprietary-self-hosted-and-managed-cloud-distribution.md).
The customer-facing, digest-pinned Compose profile and signature-verification
instructions live in [`deploy/`](deploy/README.md); the root Compose file
remains the source-workspace development profile.

## Local setup

Requirements:

- Python 3.12+
- Docker with Compose
- a GitHub or GitLab token that can clone private target repositories
- a GitHub App installation or user access token with pull-request read/write
  permission, plus Checks write permission when status checks are enabled
- or a GitLab token with API access that can read merge requests, diff
  versions, project membership, discussions, and emoji; create/reply to/resolve
  merge-request discussions; update MR descriptions when configured; approve
  eligible merge requests; and publish commit statuses when enabled
- an embedding/model provider supported by LiteLLM

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, DIFFUSE_API_TOKEN, the model credentials,
# and the applicable SCM token and webhook credentials.

docker compose up -d --build

docker compose run --rm migrate database status

docker compose run --rm worker repository add \
  --provider github \
  --base-url https://github.com \
  --repo owner/repository \
  --default-branch main
```

The `add` command clones the default branch into the shared repository volume
and queues its initial index. Use `list`, `sync <repository-id>`,
`disable <repository-id>`, and `enable <repository-id>` for basic lifecycle
operations.

Before onboarding, Diffuse requires the exact SCM origin to match the
provider's configured primary origin or explicit allowlist. Set
`GITHUB_WEB_URL`/`GITHUB_API_URL` or
`GITLAB_WEB_URL`/`GITLAB_API_URL` for a primary self-managed host. Add any
additional exact origins to `GITHUB_ALLOWED_INSTANCES` or
`GITLAB_ALLOWED_INSTANCES`; these hosts receive the applicable process-level
provider token during clone/fetch, so keep the lists narrow.

Compose runs a one-shot migration service after PostgreSQL is healthy and
starts the API and worker only after that service exits successfully. Every
migration runs in one transaction behind a PostgreSQL advisory lock. Applied
version, immutable SHA-256 checksum, actor, duration, and whether an existing
baseline was adopted are stored in `diffuse_schema_migrations`; retries are
no-ops, concurrent migrators serialize, and checksum drift fails closed.
`GET /health` remains a process liveness probe; `GET /ready` returns `200` only
when the packaged migration history and baseline database contract are current.

Back up PostgreSQL before upgrading. Inspect or verify schema state with:

```bash
docker compose run --rm migrate database status
docker compose run --rm migrate database verify
```

An installation created before versioned migrations has application tables but
no migration ledger. The migrator refuses to guess. After inspecting and
backing up that database, explicitly adopt it; Diffuse first verifies every
version-1 table and column plus pgvector:

```bash
docker compose run --rm migrate database migrate \
  --adopt-existing \
  --actor operator@example.com
```

`sql/schema.sql` is the frozen version-1 migration. New releases append
consecutive files under `sql/migrations/`; editing any applied file or its
recorded checksum prevents startup.

The production image installs the same `diffuse` command used above. Install it
into a local Python environment with `uv pip install -e .` (or an equivalent
Python installer) to manage the self-hosted service and review a local branch
using the same index, policy, learned rules, retrieval, and native verifier:

```bash
diffuse repository list
diffuse cluster list
diffuse learning list 1
diffuse review
diffuse review -b origin/main --diff
diffuse review --json
diffuse review --agent
diffuse review --resume
diffuse model
diffuse model --live
diffuse evaluate evals/baseline.example.json
```

The checkout must correspond to an enabled, indexed Diffuse repository. The
CLI identifies it from `origin`; use `--repo owner/repository` and
`--scm-base-url https://github.example.com` when multiple registered hosts are
ambiguous. By default it reviews tracked committed, staged, and unstaged
changes against the merge base. Untracked files are reported but excluded
unless `--include-untracked` is explicitly supplied.

`--diff` adds exact diff excerpts, `--json` emits the versioned
`diffuse-cli-review-v1` document, and `--agent` emits terminal-safe plain text
with every finding, evidence item, and suggested fix. `--fail-on-findings`
returns exit status 1 for CI or scripts. An interrupted or failed command
stores no source or model output, only bounded review identity under the Git
common directory. `--resume` retries only if the repository, diff, base,
untracked choice, index snapshot, policy fingerprint, model, and prompt version
are unchanged; completed or drifted runs require a new review.

`diffuse model` reports the selected LiteLLM provider, expected credential
variable names, and readiness booleans without printing secret values.
`diffuse model --live` makes a small schema-validated request and should be run
before onboarding the first review repository.

The versioned evaluation format under `evals/` matches labeled and observed
findings one-to-one by category, path, and bounded line tolerance. It reports
true bugs, false positives, false negatives, developer-addressed findings,
precision, recall, F1, median latency, token use, and estimated cost:

```bash
diffuse evaluate evals/baseline.example.json \
  --min-precision 0.80 \
  --min-recall 0.60
```

The committed set is intentionally synthetic. Replace it with reviewed pull
requests before using the thresholds as a product-quality claim.

### MCP

Diffuse serves a stateless JSON Streamable HTTP MCP endpoint at `/mcp`.
`DIFFUSE_API_TOKEN` is a high-entropy bootstrap/recovery credential with
installation-wide access; set it to at least 32 visible ASCII characters and
keep it in the deployment secret manager. Routine clients should use durable,
least-privilege service tokens. When the public host is not localhost, also
set `DIFFUSE_PUBLIC_URL` to its HTTP(S) origin and add the exact host
(including its port when applicable) to the comma-separated
`DIFFUSE_MCP_ALLOWED_HOSTS` allowlist.

`diffuse token add` mints the credential itself with a CSPRNG, prints it once,
and stores only its SHA-256 digest. Capture that single line into the client's
secret manager; Diffuse cannot show it again:

```bash
diffuse token add ide-agent \
  --scope diffuse:mcp:read \
  --scope diffuse:mcp:generate \
  --repository-id 1 \
  --actor operator

diffuse token list
diffuse token revoke 1 --actor operator --reason "credential rotation"
```

`--token-env DIFFUSE_NEW_TOKEN` still adopts an operator-supplied credential
from an environment variable, but prefer minting: a single unsalted SHA-256 is
only sound for a high-entropy secret, and an operator-chosen string in that
column is recoverable offline from any dump or read replica.

Use `--all-repositories` instead of one or more `--repository-id` values only
for clients that genuinely require deployment-wide access. Creation and
revocation append immutable audit events. Token listings expose lifecycle and
scope metadata but never credentials or hashes. Add
`--scope diffuse:mcp:generate` only for clients allowed to spend model capacity
on repository Q&A, and add `--scope diffuse:mcp:write` only for clients allowed
to trigger reviews or create custom context. Plain source search requires only
read scope; read-only credentials cannot invoke generation or write tools.

For example, an MCP-compatible Codex client can use:

```bash
codex mcp add diffuse \
  --url https://diffuse.example.com/mcp \
  --bearer-token-env-var DIFFUSE_MCP_TOKEN
```

The current server advertises twenty-one tools:

- repository discovery, repository-authorized `get_review_analytics`,
  commit-pinned `search_code`, citation-grounded `ask_codebase`, both
  `list_merge_requests` and `list_pull_requests`, and pull-request detail;
- review list/detail plus authoritative GitHub and GitLab re-run triggers;
- PR comment projection plus both the public `search_greptile_comments`
  compatibility name and the Diffuse-native repository-filterable search;
- custom-context list/detail/search/create plus Diffuse-native optimistic
  update/delete; and
- revision-safe `get_fix_handoff` and `get_fix_all_handoff` bundles.

The public pull-request, review, and comment tools accept repository
descriptors using `name`, `remote`, `defaultBranch`, optional `remoteUrl`, and
`prNumber`, matching the documented MCP wire shape. The compatibility
`greptileGenerated` field means “generated by the configured review system” on
this self-hosted endpoint; every projected finding also carries the unambiguous
`diffuseGenerated: true` field. `list_repositories` exposes the exact
descriptors available to a token.

Results come from durable pull-request state, review runs, published finding
lineages, active immutable snapshots, operator context, and inspectable
feedback-derived rules. Every read and write is constrained to the
repositories assigned to the authenticated token. `search_code` fuses literal
identifier, semantic, and one-hop graph evidence within an optional literal
path scope and returns immutable GitHub/GitLab commit permalinks.
`ask_codebase` uses the same bounded evidence but emits claim-level citations;
claims with a missing, ambiguous, out-of-range, or unauthorized citation are
dropped, and no usable claims produces an explicit insufficient-evidence
response. Optional repository-cluster search is intersected with the token's
repository grants. Model generation has its own scope, output/token limit, and
timeout. Review triggering fetches
the current GitHub PR head before queuing work, and closed/merged webhook state
cancels queued reviews. Active custom context is path-scoped, included in the
review-policy fingerprint and prompt, and snapshotted onto each review run.
Published GitHub reviews identify the exact MCP handoff call for each finding
and for Fix All. A handoff is available only while the review's base and head
still match an open PR and the finding remains the latest active lineage
occurrence. It includes file/line/side, evidence, suggested fix, immutable
review guidance, SCM identity, and an agent-ready prompt that requires checkout
verification, minimal changes, relevant tests, and human control of
commit/push. Supported target labels are Codex, Claude Code, Conductor, Cursor,
Devin, and generic MCP; no agent receives credentials and the server never
edits a developer checkout.

Operator-created custom context can be edited or permanently deleted only with
MCP write scope and the exact `updatedAt` value most recently read. Conflicting
writers fail instead of overwriting one another; no-op retries preserve the
timestamp. Updates record field names, state/scope transitions, and body/
metadata hashes in the immutable audit log. Deletes retain a hashed audit
tombstone, while prior review runs keep the exact context snapshots they used.
Feedback-derived learned rules cannot be changed through these tools and retain
their evidence/approval/version workflow.

`get_review_analytics` accepts required timezone-aware `startAt` and `endAt`,
an optional repository descriptor, and an optional case-insensitive exact
`author` filter. Its window is half-open, bounded to 366 days, and always
normalized to UTC. Without a descriptor it aggregates only repositories
assigned to the token. The report returns exact durable review attempts and
status counts, distinct PRs reviewed, opened PRs reviewed versus unreviewed,
mean/median open-to-merge time, lifecycle-timestamp completeness, completion
and latency, token usage, auto-approvals, custom-context adoption, applied
finding occurrences and unique lineages, current address/critical/security
state, category/severity counts, current upvote/downvote percentages and
context-reply engagement, repository breakdowns, UTC daily trends, and the
twenty highest-priority open findings with PR links. Every rate includes its
denominator definition, empty denominators return `null`, and current-state
metrics carry the report's database `asOf` time. Diffuse refuses to substitute
webhook receipt time when an SCM omits a lifecycle timestamp and reports the
resulting completeness explicitly. Historical policy-eligible coverage and
monetary cost remain unavailable until their exact inputs are versioned.

Organizations/team RBAC and a local custom-URL bridge for literal one-click
agent launch are not yet implemented. Diffuse already marks findings addressed
or reopened from exact subsequent review diffs and projects GitHub/GitLab thread
state through the comment tools.

Configure a GitHub webhook for `push`, `pull_request`, `issue_comment`, and
`pull_request_review_comment` events at:

```text
https://your-host.example/webhook/github
```

Use the same random value for the webhook's GitHub secret and
`GITHUB_WEBHOOK_SECRET`. Diffuse refuses webhook requests when the secret is
missing or the signature is invalid.

For GitLab Cloud or Self-Managed, enable merge-request, push, and comment
events at:

```text
https://your-host.example/webhook/gitlab
```

Preferred GitLab signing uses the Standard Webhooks `webhook-id`,
`webhook-timestamp`, and `webhook-signature` headers. Put the exact `whsec_`
token returned by GitLab in `GITLAB_WEBHOOK_SIGNING_TOKEN`; Diffuse verifies
HMAC-SHA256 over the exact raw body and rejects timestamps outside the bounded
replay window. Older installations can use `X-Gitlab-Token` with
`GITLAB_WEBHOOK_SECRET`, but must also send GitLab's stable
`Idempotency-Key` or `X-Gitlab-Event-UUID`. When Standard Webhooks headers are
present, an invalid signature never falls back to the legacy token.

Set `GITLAB_WEB_URL` and `GITLAB_API_URL` together for the primary
Self-Managed installation. `X-Gitlab-Instance` is accepted only when it exactly
matches that origin or an origin in `GITLAB_ALLOWED_INSTANCES`; this prevents a
signed payload from redirecting API enrichment to an arbitrary host. The
process-level token and webhook credentials are still shared across configured
GitLab instances pending encrypted per-installation credentials.

SCM tokens are passed to Git only through a non-interactive askpass
environment. They are not embedded in clone URLs, job payloads, database rows,
or command arguments. Repository onboarding accepts only the configured
primary origin or an exact entry in `GITHUB_ALLOWED_INSTANCES`/
`GITLAB_ALLOWED_INSTANCES`, preventing an API request from redirecting
askpass credentials to an arbitrary host. GitHub Enterprise is selected with
`GITHUB_WEB_URL` and `GITHUB_API_URL`. Set `GITHUB_GRAPHQL_URL` when its
GraphQL endpoint cannot be derived from the REST URL. GitLab cloning and API
paths support nested group namespaces. GitLab merge-request deliveries are
enriched from the REST API so the durable review always carries the
authoritative diff base/head/start, lifecycle
timestamps, source-project identity for forks, and current metadata. If GitLab
has not prepared a new MR's diff version yet, Diffuse returns a retryable 503
instead of inventing a base SHA.

A top-level GitLab merge-request comment whose line starts with `@diffuse`
requests a complete manual rerun. Diffuse accepts it only after GitLab confirms
that the author has inherited Developer-or-higher project access, re-fetches
the current open MR metadata, and uses the note ID as the distinct trigger
identity.

The version-1 database schema expects 1,536-dimensional embeddings. A release
that supports another stored dimension must add a numbered migration for both
`VECTOR(1536)` and the snapshot dimension constraint, set
`EMBEDDING_DIMENSIONS` consistently, and schedule compatible re-indexing.
Never edit the frozen baseline migration.

`REVIEW_MODEL` accepts LiteLLM model identifiers. `REVIEW_VERIFIER_MODEL`
optionally selects an independent verifier from another model family.
`REVIEW_API_BASE` can point review generation at an operator-controlled
OpenAI-compatible endpoint.
OpenAI, Anthropic, Google Gemini, Azure, AWS Bedrock, Ollama, and other LiteLLM
routes use their conventional provider configuration in the data plane; model
credentials are never projected into the optional control plane.
`REVIEW_STRUCTURED_OUTPUT_MODE=auto` uses provider-native schemas when
available and otherwise uses schema-constrained prompting with local Pydantic
validation. Invalid model output fails the durable attempt and is never posted
to the SCM.

Before generation, Diffuse deterministically inspects bounded PR/MR commit
metadata—authors, committers, bot identities, verification state, and Git
trailers such as `Co-authored-by` and `Made-with`. No model is called for this
classification. Strong Anthropic attribution selects a configured non-Anthropic
reviewer and strong OpenAI attribution selects a non-OpenAI reviewer. Cursor,
Copilot, mixed, incomplete, or absent attribution cannot weaken or skip a
review; those cases retain the configured candidate/verifier pair. Set
`REVIEW_PROVENANCE_MIN_CONFIDENCE` to control when an opposing-family route is
allowed. The evidence, confidence, selected models, and routing reason are
stored with the immutable review run.

Repository Q&A inherits `REVIEW_MODEL` and `REVIEW_API_BASE`; set
`CODE_QUERY_MODEL` to choose a different LiteLLM model. Each call is bounded by
`CODE_QUERY_MAX_OUTPUT_TOKENS` and `CODE_QUERY_MODEL_TIMEOUT_SECONDS`. Plain
`search_code` never invokes the generation model, though hybrid retrieval still
uses the configured embedding provider.

### REST API

Diffuse exposes a versioned control-plane API under `/api/v1`; its
OpenAPI document is available at `/openapi.json` and the interactive reference
at `/docs`. It uses the same bootstrap credential, hashed service-token
lifecycle, repository grants, and PostgreSQL projections as MCP. Provision a
routine client with API-specific scopes:

```bash
diffuse token add dashboard-reader \
  --scope diffuse:api:read \
  --repository-id 1 \
  --actor operator
```

Add `diffuse:api:generate` only when the client may call model-backed
repository Q&A. Q&A requires both `diffuse:api:read` and
`diffuse:api:generate`; source search requires read scope only.
An API review trigger requires both `diffuse:api:read` and
`diffuse:api:write`. Reindexing uses the same two scopes and is further
restricted by the token's repository grant. Creating a repository requires
`diffuse:admin` plus all-repositories access because a repository-specific
grant cannot safely authorize an object that does not exist yet.

The first v1 surface provides:

- repository list/detail with mirror and active immutable-index state;
- repository PR/MR list/detail, review list/detail, and current finding list;
- exact half-open review analytics with optional repository and author filters;
- commit-pinned hybrid code search; and
- evidence-bounded repository Q&A with claim-level source citations;
- an authoritative GitHub/GitLab PR/MR review trigger that re-fetches the
  current open head before enqueueing durable work;
- administrative GitHub/GitLab repository registration that validates the
  exact configured origin and clone access before queueing the resolved
  default-branch commit; and
- repository-authorized default-branch reindex requests using the same mirror,
  push-event ledger, and exact-commit worker path as authenticated webhooks.

All repository object paths use Diffuse's durable numeric repository ID. List
routes use bounded `limit`/`offset` pagination. Object lookups return the same
404 response for missing and unauthorized resources, and malformed requests
or authorization failures use `application/problem+json`.

Every repository, index, or review mutation requires a client-generated
`Idempotency-Key` of 1–200 URL-safe characters. Diffuse stores only its SHA-256
digest, scopes it to the authenticated actor and operation, rejects reuse with
a different route or body, and replays the exact completed response. Repository
index operations persist the resolved exact-commit event before queueing, so a
crash retry cannot drift to a newer branch head. An in-progress request returns
409 with `Retry-After`; a replay includes `Idempotency-Replayed: true`.

```bash
curl \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  "https://diffuse.example.com/api/v1/repositories?limit=20&offset=0"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"query":"where is tenant authorization enforced?","limit":8}' \
  "https://diffuse.example.com/api/v1/repositories/1/code/search"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Idempotency-Key: 5d47f272-c0d8-4c1f-a1df-b9561fb99d8f" \
  --header "Content-Type: application/json" \
  --data '{}' \
  "https://diffuse.example.com/api/v1/repositories/1/pull-requests/42/reviews"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_ADMIN_TOKEN}" \
  --header "Idempotency-Key: 890e7a82-43a6-485a-8c74-b7311517c90d" \
  --header "Content-Type: application/json" \
  --data '{
    "remote": "github",
    "remoteUrl": "https://github.com",
    "name": "owner/repository",
    "defaultBranch": "main"
  }' \
  "https://diffuse.example.com/api/v1/repositories"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Idempotency-Key: a16df2a6-90bb-46c5-8c00-f894e23d7214" \
  --header "Content-Type: application/json" \
  --data '{}' \
  "https://diffuse.example.com/api/v1/repositories/1/indexes"
```

## Repository review policy

Diffuse reads version-controlled policy from the same immutable commit it
indexes. Put `.diffuse/config.json` at the repository root or in any
subdirectory:

```json
{
  "version": 1,
  "review": {
    "enabled": true,
    "passes": ["correctness", "security", "tests"],
    "minimum_confidence": 0.85,
    "minimum_severity": "medium",
    "ignored_paths": ["generated/**", "**/*.snap"],
    "summary_only": false,
    "respond_to_comments": true,
    "update_description": false,
    "summary_comment": true,
    "fix_with_agent": true,
    "summary_section": {
      "included": true,
      "collapsible": false,
      "default_open": true
    },
    "issues_table_section": {
      "included": true,
      "collapsible": false,
      "default_open": true
    },
    "confidence_score_section": {
      "included": true,
      "collapsible": false,
      "default_open": true
    },
    "diagram": {
      "included": true,
      "collapsible": true,
      "default_open": true
    },
    "hide_footer": false
  },
  "triggers": {
    "automatic": true,
    "review_drafts": false,
    "review_updates": true,
    "labels": ["needs-review", "security-*"],
    "disabled_labels": ["wip-*", "do-not-review"],
    "include_authors": [],
    "exclude_authors": ["dependabot[bot]", "*-bot"],
    "include_branches": ["main", "release/{stable,latest}"],
    "exclude_branches": ["experimental/**"],
    "include_keywords": [],
    "exclude_keywords": ["do not review"],
    "file_change_limit": 250,
    "status_check": true,
    "blocking_severities": ["critical", "high"]
  },
  "context": {
    "repos": ["owner/shared-library", "owner/sdk"]
  },
  "security": {
    "preventative": false,
    "preventative_minimum_confidence": 0.9
  },
  "auto_approval": {
    "enabled": false,
    "risk_ceiling": "low",
    "filters": {
      "exclude_paths": ["src/auth/**", "db/migrations", ".github/workflows/**"],
      "exclude_authors": ["dependabot[bot]"],
      "file_change_limit": 10
    }
  },
  "rules": [
    {
      "id": "tenant-boundary",
      "title": "Preserve tenant isolation",
      "guidance": "Database reads and writes must be scoped by the authenticated tenant.",
      "applies_to": ["src/**"],
      "severity": "high",
      "category": "security"
    }
  ],
  "rule_overrides": {}
}
```

Configuration cascades deterministically from the repository root toward the
changed file. Nested scalar settings replace inherited values; ignored-path
matches accumulate; rules have stable IDs and a nested layer can change their
enabled state, severity, or category through `rule_overrides`. Globs use `/`,
`*` does not cross a directory boundary, and `**` does. Patterns in a nested
layer are relative to that layer.

Repositories migrating from Greptile can keep a tracked root
`greptile.json`. When the root has no native `.diffuse/config.json`,
`.diffuse/rules.md`, or `.diffuse/files.json`, Diffuse imports the current
public trigger/filter fields, Git-style ignore patterns, cross-repository
context, instructions, custom rules/files/text, summary-only mode, output
sections, footer, description/summary/fix publication controls, and
status-check settings into the same immutable policy snapshot. `strictness`
maps deterministically to a minimum finding severity:
`1=low`, `2=medium`, and `3=high`; the threshold is enforced after independent
verification. `commentTypes` becomes bounded review guidance while directly
evidenced security defects remain protected.

Native root `.diffuse` policy takes precedence and causes `greptile.json` to be
ignored, matching the source format's documented hierarchy. Import rejects
unknown keys, untracked context files, ignore negation/character classes, and
settings Diffuse cannot preserve safely. `shouldUpdateDescription`,
`statusCommentsEnabled`, and `fixWithAI` map to `update_description`,
`summary_comment`, and `fix_with_agent`.

Automatic reviews run for newly opened and reopened PRs by default. Draft PRs
and new commits are skipped unless `review_drafts` and `review_updates` are
enabled. Ready-for-review, label, keyword-edit, and relevant label-removal
events can create a new decision even when the commit SHA did not change.
Label, author, and target-branch patterns are case-insensitive and support
`*`, `**`, `?`, and bounded `{a,b}` alternatives; square brackets and leading
`!` are literal. Exclusion filters take precedence over inclusion filters.
Keywords are case-insensitive title/description substrings.

Trigger arrays replace inherited arrays for one file. When a PR changes files
under different configs, automatic/draft/update booleans use permissive OR
semantics, exclusion filters are combined, inclusion filters allow the PR when
any applicable path is unrestricted, and the smallest file-change limit wins.
Every denied trigger is persisted with a stable skip reason before any
embedding or review-model call.

The security pass distinguishes an exploitable `vulnerability` in the current
snapshot from a `preventative` risk that would become exploitable only after a
specific future trust-boundary change. Vulnerability review is always enabled
when the security pass applies. Preventative review is path-scoped and opt-in
through `security.preventative`; it uses the stricter of the ordinary and
preventative confidence floors. Preventative findings cannot claim critical or
high severity. Diffuse persists the classification through finding lineage,
conversation, feedback, and learning records, and labels GitHub reviews and
check annotations as either `🔒 Security vulnerability` or
`🛡️ Preventative security risk`.

`auto_approval.enabled` defaults to `false`. When requested, Diffuse records an
inspectable decision after the ordinary review and status check finish, and
submits a commit-pinned GitHub review or SHA-pinned GitLab approval only when
all of these hold:

- every touched path scope enables auto-approval;
- authoritative PR metadata and the diff are complete, including both sides of
  renames;
- all configured author, target-branch, label, keyword, repository, path, and
  file-count filters pass;
- every changed file was reviewed, no path was ignored, the current report has
  no findings, no earlier finding lineage remains open, and risk score is zero;
- Diffuse's deterministic change-risk class does not exceed the strictest
  configured ceiling.

Low covers documentation, tests, styling, and very small changes; ordinary
application changes are medium; dependency/build/runtime/shared-core changes
are high. Auth, public API, secret, billing/payment, schema/migration, CI, and
infrastructure paths are critical and are never automatically approved,
including when `risk_ceiling` is set to `critical`. Nested scopes merge
strictest-wins: every scope must enable the feature, the lowest ceiling and
smallest file limit win, exclusion filters union, and every applicable
inclusion filter must match. Approval state and attempts are durable. Before
posting, Diffuse re-fetches the PR/MR and cancels approval if it closed, became
a draft, or moved to another head commit. GitHub uses a hidden per-run/head
marker to recover remote-create crash windows. GitLab additionally waits until
`detailed_merge_status` leaves its approval-sync states and the matching diff
version has a non-null patch ID, sends the exact reviewed SHA, verifies the
authenticated bot in `approved_by`, and safely recovers an already-present bot
approval only after that exact-SHA request. A GitLab SHA conflict cancels the
stale action.

`context.repos` also replaces its inherited value for the applicable path.
Every entry must be an explicitly onboarded, enabled repository on the same
SCM provider and exact host as the reviewed repository. An explicit repository
without a compatible active index fails the review closed. The review pins its
exact repository, commit, and snapshot before retrieval, so an index update
during model generation cannot change the review's evidence.

Operators can also group related onboarded repositories without changing every
repository's committed configuration:

```bash
docker compose run --rm worker cluster create product-stack \
  --repository-id 1 \
  --repository-id 2 \
  --repository-id 3 \
  --actor operator@example.com
docker compose run --rm worker cluster list
docker compose run --rm worker cluster add 1 4 \
  --actor operator@example.com
docker compose run --rm worker cluster remove 1 4
docker compose run --rm worker cluster delete 1
```

Cluster members must share one SCM provider and host. Explicit entries take
precedence, cluster membership adds deduplicated repositories, and the combined
plan is capped at seven related repositories. Disabled or not-yet-indexed
cluster members are skipped; explicit entries fail closed so a committed
dependency cannot disappear silently. Related repositories contribute
read-only lexical and semantic reference chunks. They do not create synthetic
cross-repository graph edges, and findings still must point to changed lines in
the primary pull request.

`status_check` defaults to `false`. When enabled for any reviewable changed
path, Diffuse creates an in-progress `Diffuse code review` check on the exact
head commit and completes it after review publication. Findings whose severity
is listed in `blocking_severities` fail the check; other completed reviews pass.
Nested status-check scopes combine blocking severities conservatively. A newer
PR event cancels an in-progress check, and terminal workflow failure fails it.
Creation and completion are persisted with a stable external key so worker
retries recover the same GitHub check instead of creating duplicates.

Every published review includes a deterministic 0–5 confidence score alongside
the independent 0–10 risk score. Confidence starts from verified risk, then
tightens for finding volume, incomplete diff coverage, ignored files, or no
reviewed files; it is never taken directly from untrusted model prose. A score
of 5 therefore means a clean, fully covered review, while automatic approval
also requires every other approval gate to pass.

At publication time Diffuse assigns a per-pull-request review number while
holding the pull request's database lock. Retries reuse that number. The review
footer shows the durable counter, links the exact reviewed commit, and gives
the working `@diffuse review` re-trigger command.

For non-trivial changes, Diffuse can make one additional structured model call
to propose a sequence, entity-relation, class, or flow diagram. Small changes
never invoke the diagram stage, and the model may return no diagram when the
relationships are not grounded. Accepted Mermaid is limited to the matching
diagram directive and strict line/character budgets; links, callbacks, init
directives, URLs, HTML payloads, styling directives, and Markdown fences are
rejected before persistence. Configure `review.diagram.included`,
`collapsible`, and `default_open` in cascading policy. If any reviewable
touched path disables inclusion, the PR has no diagram.

The summary, issues table, confidence score, and diagram sections each support
`included`, `collapsible`, and `default_open`; `hide_footer` removes the review
counter/commit/re-trigger footer. Settings resolve per changed path and the
PR-level presentation is conservative: any touched scope can hide a section,
any scope can request collapse, and every scope must request default-open for
an expanded collapsible section. The resolved presentation is stored on the
review run, so a retry cannot drift after configuration changes. Hiding the
issues table never hides a validated finding when inline publication fails or
summary-only mode is active—the detailed fallback remains mandatory.

`update_description` places the complete review summary in one managed region
of the GitHub PR or GitLab MR description while preserving all human-authored
text. The region is replaced idempotently on retries and is written only after
the provider confirms that the PR/MR is still open at the reviewed head.
`summary_comment=false` suppresses the visible top-level review summary without
suppressing eligible exact-line findings, and `fix_with_agent=false` hides
published fix-one/fix-all guidance and suggested-fix blocks without deleting
the durable finding evidence. Description output takes precedence over the
summary-comment setting. Diffuse ignores the provider webhook generated solely
by its managed-region write, but still reviews genuine human description edits.

When reviews run on later commits, Diffuse compares the previously published
head with the new head and maintains finding lineage across line movement and
minor wording changes. A finding remains open when it is detected again. When
a pushed commit touches its file and the verifier no longer detects it, Diffuse
posts one idempotent addressed reply and resolves the original GitHub or GitLab
thread. If the finding returns, Diffuse reopens that same thread and records the
transition. Only genuinely new lineages create new inline comments; persistent
findings do not create duplicates.

Lineage changes are provisional until the commit-pinned review is durably
published. Superseded or terminally failed reviews discard their provisional
events. An enabled status check considers every active lineage, so an unresolved
blocking finding from an earlier review cannot silently pass because a later
model call omitted it.

An authorized repository member can deliberately bypass all automatic trigger
filters—including draft and update gates—by starting a top-level PR/MR comment
line with `@diffuse`. GitHub requires a human owner/member/collaborator; GitLab
requires inherited Developer-or-higher project access. Diffuse fetches fresh
metadata for the open PR/MR from the configured provider API, and the comment
or note ID gives every manual rerun a distinct idempotency identity. Manual
questions in a top-level comment still start a complete review.

On an inline finding created by Diffuse, an authorized repository member can
reply with `@diffuse <question>` to ask for clarification, alternatives,
testing guidance, or related repository patterns. Diffuse verifies that the
root is one of its stored finding threads and checks GitHub owner/member/
collaborator authority or GitLab inherited Developer-or-higher membership. It
then retrieves hybrid graph/lexical/vector context around the finding, includes
prior published turns, and posts a structured answer with only validated code
references into the same provider-native thread. Questions on the same thread
are processed in order. Durable message state plus a hidden marker recovers
both model/publication retries and the remote-create crash window without
duplicate answers.

Bots, unrelated threads, unassociated users, acknowledgements such as
`@diffuse thanks`, comments without the explicit mention, and comments starting
with `[Human discussion only]` remain silent. Set `respond_to_comments` to
`false` in the applicable path's cascading review policy to disable replies.

Authorized human replies on Diffuse finding threads are also stored as
inspectable context signals, even when they do not mention `@diffuse`.
`[Human discussion only]` excludes a reply from both inference and feedback
capture. The worker schedules low-priority provider API reconciliation jobs so
retries and reaction removals have one consistent durable model. Only current
👍 and 👎 reactions from GitHub collaborators or GitLab Developer-or-higher
members become positive or negative signals; other emoji and unauthorized
actors are neutral, removed reactions are recorded as withdrawn, and
addressed/reopened commit outcomes are retained separately.

Configure reaction reconciliation with
`FEEDBACK_SYNC_INTERVAL_SECONDS` (60–86400),
`FEEDBACK_SYNC_SCHEDULER_SECONDS`, and `FEEDBACK_SYNC_BATCH_SIZE` (1–100).
Diffuse stores source identity, actor authority, finding category/severity,
and an immutable transition history. Security, correctness, and critical
signals are marked as protected from future preference suppression. This
foundation does not automatically suppress findings.

After the configured minimum history (10 feedback events across 10 PRs by
default), the worker may generate deduplicated rule suggestions. Every
suggestion cites durable feedback event IDs and remains inert until an operator
approves it. Suggestions may be inspected, edited, approved, rejected,
deactivated, and reactivated with the operator CLI:

```bash
docker compose run --rm worker learning list 1
docker compose run --rm worker learning show 1 4
docker compose run --rm worker learning edit 1 4 \
  --expected-version 1 \
  --actor operator@example.com \
  --guidance "API handlers must use the shared request validator."
docker compose run --rm worker learning approve 1 4 \
  --expected-version 2 \
  --actor operator@example.com
```

The first positional value is the repository ID and the second is the learned
rule ID. The CLI treats authenticated shell access to the self-hosted worker as
the authorization boundary and records the supplied actor as an `OPERATOR`.
Approval changes the effective policy fingerprint for future reviews; each
review run snapshots the exact approved versions it used. Repository-authored
rules take precedence on conflict. Configure scheduling and evidence thresholds
with the `RULE_LEARNING_*` and `SUGGESTED_RULE_*` variables in `.env.example`.

Free-form scoped guidance can live in `.diffuse/rules.md`. Explicit context
files are declared in `.diffuse/files.json`:

```json
{
  "version": 1,
  "files": [
    {
      "path": "docs/security-model.md",
      "description": "Repository trust boundaries",
      "applies_to": ["src/auth/**", "src/api/**"]
    }
  ]
}
```

The context path is relative to the layer containing `.diffuse`. Diffuse also
discovers scoped `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.cursorrules`,
`.cursor/rules/*.mdc`, and `.github/copilot-instructions.md` files. Cursor MDC
files support a bounded one-line `globs` front-matter field.

Only tracked, regular UTF-8 files are accepted. Individual policy/context
sources and the aggregate policy have strict size limits; duplicate JSON keys,
unknown rule overrides, parent traversal, absolute paths, untracked context,
and invalid schemas fail indexing. Policy is fingerprinted and persisted in
the index snapshot. Reviews exclude ignored/disabled files before retrieval,
apply path-specific confidence floors after independent verification, suppress
inline comments in summary-only mode, and publish nothing when all changed
files are disabled.

## Development

```bash
pip install -r requirements-dev.txt
pip install --no-deps -e .
pytest -m "not integration"
ruff check .
pip-audit -r requirements.lock --disable-pip
```

With a disposable pgvector database available:

```bash
POSTGRES_TEST_DATABASE_URL=postgresql://... pytest -m integration
```

`tests/integration/conftest.py` also routes application database connections to
that disposable URL. Production images install the hash-locked
`requirements.lock`. After intentionally changing runtime dependency ranges,
regenerate and review it with:

```bash
pip-compile requirements.txt \
  --output-file=requirements.lock \
  --generate-hashes \
  --allow-unsafe \
  --strip-extras
```

To run the API directly while keeping Postgres in Docker:

```bash
set -a
source .env
set +a
uvicorn service.webhook_server:app --reload
```

## Current foundation

Diffuse currently:

- indexes clean Git commits into immutable, model-pinned snapshots;
- atomically activates complete snapshots while retaining prior commit indexes
  for reproducibility and reusing unchanged embeddings;
- extracts persisted symbols and containment/import/call/inheritance/
  implementation edges for Python, JavaScript/JSX, TypeScript/TSX, Go, Java,
  Ruby, Rust, PHP, C, and C++;
- derives chunk boundaries from parser-backed definitions and isolates syntax
  failures to the affected file;
- expands retrieval through one-hop callers, callees, imports, and inheritance
  and fuses graph evidence with weighted path/symbol/content lexical search and
  semantic similarity;
- resolves cascading `context.repos` and operator-managed same-host repository
  clusters into a bounded immutable context plan, searches related snapshots
  read-only, preserves repository-qualified path provenance, and snapshots the
  exact related commits on each review run;
- persists normalized GitHub/GitLab deliveries and exact PR/MR revisions,
  deduplicates delivery retries and repeated revisions, preserves authoritative
  lifecycle events, and supersedes older queued heads;
- explicitly registers GitHub/GitLab repositories and maintains credential-safe
  bare mirrors plus disposable exact-commit worktrees, with CLI and
  admin-authorized idempotent REST onboarding constrained to configured SCM
  origins;
- queues idempotent default-branch indexing from authenticated GitHub/GitLab
  push events and serializes work for each provider/host/repository ref;
- runs review work in a separate leased PostgreSQL worker with bounded
  exponential retry and terminal failed/dead states;
- runs bounded correctness, security, performance, and test/contract passes,
  grounds every candidate to an exact changed line, deduplicates candidates,
  and subjects them to an independent conservative verifier;
- auto-selects a bounded sequence, entity-relation, class, or flow diagram only
  for non-trivial changes, validates the Mermaid safety/type contract, stores
  it durably, and renders it with cascading section presentation controls;
- classifies current security vulnerabilities separately from opt-in
  preventative risks, applies path-scoped confidence and severity safety
  floors, persists the subtype through review memory, and labels both review
  comments and check annotations;
- discovers tracked cascading `.diffuse` configuration or a compatible root
  `greptile.json`, stable scoped rules, referenced context, and common
  agent/editor instruction files as immutable snapshot data, then enforces path
  ignores, pass selection, confidence/severity floors, review disablement, and
  summary-only mode before publication;
- normalizes bounded PR author/branch/label/title/draft and authoritative
  changed-file-count metadata, enforces
  cascading automatic trigger filters before retrieval, supports same-SHA
  ready/label transitions plus authorized GitHub/GitLab `@diffuse` manual
  reruns, and
  records stable skip reasons without publishing noise;
- persists versioned review runs, findings, risk, 0–5 confidence, durable
  per-PR review numbers, token counts, immutable index and policy provenance,
  skipped decisions, and publication attempts;
- publishes a commit-pinned GitHub review with summary, issue table, confidence,
  severity, category, suggested fixes, exact LEFT/RIGHT inline comments, and a
  footer linking the reviewed commit with a working re-trigger command; policy
  can instead place the summary in a human-preserving managed PR-description
  region, suppress the visible summary, or hide fix guidance;
- publishes the same validated report to GitLab with exact version-pinned
  added/removed-line discussions and an idempotent commit-linked summary or
  managed MR-description region;
  invalid/unavailable positions fail safe to complete finding details in the
  summary;
- snapshots cascading summary/issues/confidence/diagram/footer presentation
  controls plus description/summary/fix publication controls on each report
  and preserves mandatory fallback finding details on an enabled summary
  surface;
- installs a unified `diffuse` CLI for repository onboarding/lifecycle,
  cross-repository clusters, learned-rule moderation, and local review; local
  review resolves the merge base, reuses immutable index/cross-repository
  context/approved memory, supports inline, JSON, and agent output, and safely
  resumes unchanged failed runs;
- serves a bearer-authenticated, DNS-rebinding-protected Streamable HTTP MCP
  foundation with pull-request list/detail, authoritative GitHub/GitLab re-runs,
  review reports/findings, commit-pinned hybrid code search, citation-grounded
  repository Q&A, repository-authorized durable review analytics, and
  first-class custom context, backed by non-recoverable repository-scoped
  service tokens, separate generation/write scopes, and audited
  create/update/delete lifecycle commands with optimistic concurrency;
- serves a versioned bearer-authenticated REST API for repository/index state,
  PRs, reviews, findings, analytics, hybrid code search, and grounded Q&A over
  the same repository-scoped service-token and PostgreSQL authorization path,
  plus audited, durably idempotent exact-commit onboarding/reindexing and
  provider-revalidated manual review triggers, a separate generation scope,
  and stable Problem Details failures;
- optionally publishes a commit-pinned GitHub check or GitLab commit status
  with configurable blocking severities, retry recovery, and deterministic
  success/failure/cancellation conclusions; exact-line status annotations are
  currently GitHub-only;
- conservatively auto-approves fully reviewed, clean GitHub/GitLab changes only
  after strictest-wins path policy, hard critical-surface protections, inherent
  risk classification, exact-head revalidation, provider-specific
  synchronization guards, and durable idempotent publication;
- tracks durable finding lineages across commits, classifies new/persistent/
  addressed/reopened findings, avoids duplicate inline comments, and
  idempotently resolves or reopens the original GitHub/GitLab review thread;
- accepts explicit authorized `@diffuse` questions on Diffuse-owned
  GitHub/GitLab finding threads, serializes turns, retrieves snapshot-compatible
  repository context, filters unsupported model references, and publishes
  same-thread replies with durable retry/crash recovery;
- captures authorized finding-thread replies, periodically reconciles
  collaborator 👍/👎 reactions, records reaction withdrawals and commit
  outcomes, and exposes inspectable per-finding summaries with hard
  security/correctness/critical protection metadata;
- durably infers evidence-cited suggested rules after a configurable history
  threshold, consolidates duplicates, records immutable edit/approval/rejection/
  activation history, applies only active human-approved versions, and
  snapshots those versions onto review runs;
- recovers GitHub and GitLab publication crash windows through hidden
  idempotency markers and managed description regions, suppresses self-induced
  description webhook loops, and falls back to complete summary details when
  GitHub rejects inline positions or GitLab rejects an exact discussion
  position;
- uses parser-backed definition boundaries with bounded line-window fallback,
  without claiming compiler-grade type analysis;
- retrieves by cosine similarity from one embedding of the meaningful diff
  lines, while exact code identifiers remain recoverable through PostgreSQL
  full-text search;
- supports GitHub and GitLab PR/MR and default-branch push webhooks, review
  publication, provider-native status output, grounded finding-thread replies,
  and authorized feedback ingestion.

It does not yet perform multi-hop retrieval, expose workflow administration,
use stored SCM App/OAuth installation credentials, or answer top-level or
arbitrary-line questions inside SCM threads. Top-level manual commands still
trigger a full review rather than a focused instruction.
Feedback-driven noise ranking, organization/team moderation APIs and UI, additional languages,
generated summaries, richer usage/type edges, and retrieval-quality evaluation
remain.

Before a production deployment, add encrypted SCM App/OAuth installation
onboarding, migration rollback/backup drills, workflow cancellation and
operator visibility, tenant authorization, and operational metrics.

## Licensing

Diffuse does not yet include its own license file; choose one before
distributing this repository.
