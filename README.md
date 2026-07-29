<p align="center">
  <img src="assets/logo/mark-black-on-acid-rounded.svg" width="120" height="120" alt="Diffuse">
</p>

<h1 align="center">Diffuse</h1>

<p align="center">
  <strong>Code review for the age of AI.</strong>
  <br>
  Self-hosted, codebase-aware review that keeps your source and your workflow under your control.
</p>

<p align="center">
  <a href="#why-diffuse">Why Diffuse</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#local-setup">Get started</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="DEVELOPMENT.md">Development</a>
</p>

---

AI can write code faster than teams can confidently review it. Diffuse closes
that gap with repository-aware pull-request review grounded in the exact code
being changed and the wider system around it.

Diffuse combines graph, lexical, and semantic retrieval with structured review
passes, then publishes commit-pinned findings directly to GitHub. It learns from
inspectable team feedback, carries context across repositories, and exposes the
same intelligence through its CLI, REST API, and MCP server.

You run the API, workers, PostgreSQL/pgvector, repository storage, and model
connections inside infrastructure you control. There is no hosted control plane
and no Diffuse account to create.

## Why Diffuse

| | |
| --- | --- |
| **Review with context** | Understands symbols, imports, calls, inheritance, and related repositories instead of reviewing an isolated diff. |
| **Find the signal** | Runs focused correctness, security, performance, and test passes, then independently verifies candidates before publishing. |
| **Stay in control** | Keeps source-derived data in your environment unless you explicitly configure an external model or integration. |
| **Improve with use** | Turns authorized replies and reactions into inspectable rule suggestions that require human approval before activation. |
| **Meet developers where they work** | Publishes native GitHub reviews and checks, with the same review context available through CLI, REST, and MCP. |

> [!NOTE]
> Diffuse is a working foundation under active development. See the
> [capability ledger](docs/capabilities.md) for what is implemented today, the
> [target architecture](docs/architecture.md) for where it is headed, and the
> [delivery roadmap](docs/roadmap.md) for the path between them.

## How it works

```text
Repository onboarding CLI or versioned REST API
  ├─ registers an explicit GitHub repository
  ├─ derives a credential-free HTTPS clone URL
  ├─ maintains a locked bare mirror
  └─ queues the exact default-branch commit for indexing

GitHub push / PR / review-thread webhook and reaction ingestion
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

## Deployment model

Diffuse is self-hosted. You run the API, workers, PostgreSQL/pgvector,
repository storage, and model connections in infrastructure you control. There
is no Diffuse-hosted control plane, no account to create, and no managed
service. Source-derived data does not leave your environment unless you
explicitly configure an external model or integration, and the model
credentials Diffuse uses are your own.

Permitted self-hosted use requires no separate commercial agreement, license
key, or registry credential. Diffuse contains no license-enforcement code.

Diffuse is **proprietary, source-available software — not open source**. The
Business Source License 1.1 is not OSI-approved. You may read, modify, and run
this software in production, including inside a commercial organization. You
may not offer it to third parties as a hosted service before the Change Date. See
[`LICENSE`](LICENSE) and
[ADR 0042](docs/adr/0042-source-available-self-hosted-distribution.md).

The digest-pinned Compose profile and signature-verification instructions live
in [`deploy/`](deploy/README.md); the root Compose file remains the
source-workspace development profile.

## Local setup

Requirements:

- Python 3.12+
- Docker with Compose
- a GitHub App installed on the target repositories, with its client/App ID,
  installation ID, and private key available to Diffuse
- an embedding/model provider supported by LiteLLM

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, DIFFUSE_API_TOKEN, the model credentials,
# and the GitHub App credentials and webhook secret.

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
configured primary origin or explicit allowlist. Set
`GITHUB_WEB_URL`/`GITHUB_API_URL` for a primary GitHub Enterprise host. Add any
additional exact origins to `GITHUB_ALLOWED_INSTANCES`; these hosts receive the
resolved GitHub credential during clone/fetch, so keep the list narrow.

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

The production image's `diffuse` entrypoint is a superset of the packaged CLI:
alongside the subcommands below it takes `serve`, `worker`, and `healthcheck`,
which is how Compose starts the API and worker. A `pip install` of this package
maps `diffuse` to the CLI only, so `diffuse serve` outside the image exits with
`invalid choice: 'serve'`.

Install the CLI into a local Python environment with `uv pip install -e .` (or
an equivalent Python installer) to manage the self-hosted service and review a
local branch using the same index, policy, learned rules, retrieval, and native
verifier:

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
diffuse evaluate evals/baseline.example.json
```

**`diffuse evaluate` scores a run; it does not perform one.** There is no
harness that drives the review engine against fixtures: `observed` is
transcribed by hand into the input file. Nothing in this repository measures
review quality automatically, and no precision or recall figure has been
recorded from a real run.

The committed set is synthetic, and deliberately records zero observed
findings against one expected finding — so it scores 0% recall and **exits
non-zero under any recall gate**:

```bash
# Exits 1 against the shipped fixture, by design.
diffuse evaluate evals/baseline.example.json \
  --min-precision 0.80 \
  --min-recall 0.60
```

Replace it with reviewed pull requests before using the thresholds as a
product-quality claim. Inside the release image the fixture is packaged at an
absolute path: `/opt/diffuse/_internal/evals/baseline.example.json`.

`diffuse review` runs policy discovery, retrieval, and several model passes, so
it can take minutes. Progress is written to **stderr**, and only when stderr is
a terminal. The `--json` and `--agent` stdout documents are therefore
byte-identical interactively and in CI, and CI logs stay free of progress
noise. Run `diffuse review --help` (or `--help` on any subcommand) for the
documented flags, worked examples, and this exit-code table.

#### Exit codes

Every `diffuse` command uses the same table, so scripts can branch on failure
class instead of parsing stderr:

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | Success | The command completed; a review may still report findings |
| `1` | Findings reported | `diffuse review --fail-on-findings` found at least one finding |
| `2` | Usage error | Unknown flag, missing argument, or an invalid argument value such as a malformed `--base` |
| `3` | Configuration or environment error | `DATABASE_URL` unreachable, missing or rejected `OPENAI_API_KEY`, repository not onboarded, no compatible index |
| `4` | Internal error | An unexpected Diffuse defect; please report it |

Failures print a single `diffuse: error: <message>` block on stderr, with
multi-line tool output indented beneath it and no traceback or irrelevant usage
dump. Messages name the environment variable to fix and never print a
credential: connection URLs are shown with the password replaced by `***`. Set
`DIFFUSE_CLI_TRACEBACK=1` to re-raise the original exception when filing a bug.

A CI gate therefore looks like:

```bash
diffuse review --json --fail-on-findings > review.json
case $? in
  0) echo "clean" ;;
  1) echo "findings reported"; exit 1 ;;
  *) echo "diffuse could not run"; exit 1 ;;
esac
```

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

The current server advertises twenty tools:

- repository discovery, repository-authorized `get_review_analytics`,
  commit-pinned `search_code`, citation-grounded `ask_codebase`,
  `list_pull_requests` alongside the identical `list_merge_requests`, and
  `get_merge_request` for pull-request detail;
- review list/detail plus an authoritative re-run trigger;
- `list_merge_request_comments` for PR comment projection plus
  repository-filterable `search_review_comments`;
- custom-context list/detail/search/create plus Diffuse-native optimistic
  update/delete; and
- revision-safe `get_fix_handoff` and `get_fix_all_handoff` bundles.

The `merge_request` names predate GitHub-only support (ADR 0041) and describe
GitHub pull requests. `list_pull_requests` is the only one with a
pull-request-named form; `get_merge_request` and `list_merge_request_comments`
have none. Renaming them would break existing clients, so they stay as they are
until a deliberate contract change.

The pull-request, review, and comment tools accept repository descriptors using
`name`, `remote`, `defaultBranch`, optional `remoteUrl`, and `prNumber`. Every
projected finding carries `diffuseGenerated`, which means the comment was
authored by the review system rather than a human. `list_repositories` exposes
the exact descriptors available to a token.

Results come from durable pull-request state, review runs, published finding
lineages, active immutable snapshots, operator context, and inspectable
feedback-derived rules. Every read and write is constrained to the
repositories assigned to the authenticated token. `search_code` fuses literal
identifier, semantic, and one-hop graph evidence within an optional literal
path scope and returns immutable GitHub commit permalinks.
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
or reopened from exact subsequent review diffs and projects GitHub thread
state through the comment tools.

Configure a GitHub webhook for `push`, `pull_request`, `issue_comment`, and
`pull_request_review_comment` events at:

```text
https://your-host.example/webhook/github
```

Use the same random value for the webhook's GitHub secret and
`GITHUB_WEBHOOK_SECRET`. Diffuse refuses webhook requests when the secret is
missing or the signature is invalid.

GitHub installation tokens are minted from the configured App private key,
cached in memory, and refreshed before their one-hour expiry. SCM tokens are
passed to Git only through a non-interactive askpass
environment. They are not embedded in clone URLs, job payloads, database rows,
or command arguments. Repository onboarding accepts only the configured
primary origin or an exact entry in `GITHUB_ALLOWED_INSTANCES`, preventing an
API request from redirecting askpass credentials to an arbitrary host. GitHub
Enterprise is selected with `GITHUB_WEB_URL` and `GITHUB_API_URL`. Set
`GITHUB_GRAPHQL_URL` when its GraphQL endpoint cannot be derived from the REST
URL. This self-hosted profile still configures one GitHub App installation per
Diffuse deployment; selecting installation identity per tenant is future work.

The version-1 database schema expects 1,536-dimensional embeddings. A release
that supports another stored dimension must add a numbered migration for both
`VECTOR(1536)` and the snapshot dimension constraint, set
`EMBEDDING_DIMENSIONS` consistently, and schedule compatible re-indexing.
Never edit the frozen baseline migration.

`REVIEW_MODEL` accepts LiteLLM model identifiers. `REVIEW_VERIFIER_MODEL`
optionally selects an independent verifier from another model family.
OpenAI, Anthropic, Google Gemini, Azure, AWS Bedrock, Ollama, and other LiteLLM
routes use their conventional provider configuration in the data plane. A
provider prefix Diffuse does not name explicitly—`mistral/…`, `groq/…`,
`xai/…`—is treated as a managed provider and requires that provider's
conventional `<PROVIDER>_API_KEY`; `diffuse model` reports it as unconfigured
until the key is set.

`REVIEW_API_BASE` points review generation at an operator-controlled
OpenAI-compatible endpoint. It applies only to identifiers that name such an
endpoint: an unprefixed deployment name, or a self-hosted LiteLLM prefix
(`ollama/`, `ollama_chat/`, `hosted_vllm/`, `vllm/`, `lm_studio/`,
`litellm_proxy/`, `openai_like/`, `custom_openai/`, and `openai/`). Pairing a
self-hosted model with a managed one therefore never sends the managed model's
name or credential to the local endpoint.
`REVIEW_STRUCTURED_OUTPUT_MODE=auto` uses provider-native schemas when
available and otherwise uses schema-constrained prompting with local Pydantic
validation. Invalid model output fails the durable attempt and is never posted
to the SCM.

Before generation, Diffuse deterministically inspects bounded pull-request
commit metadata—authors, committers, bot identities, verification state, and Git
trailers such as `Co-authored-by` and `Made-with`. No model is called for this
classification. Strong Anthropic attribution makes a configured non-Anthropic
model the candidate generator, and strong OpenAI attribution makes a non-OpenAI
model the generator; the other configured model stays on as the independent
verifier, so routing reorders the pair rather than collapsing it. Cursor,
Copilot, mixed, incomplete, or absent attribution cannot weaken or skip a
review; those cases retain the configured candidate/verifier pair.

Only identities GitHub itself asserts—a bot login, or an agent email on a
verified-signature commit—can reach the routing threshold. Git author
names, emails, and trailers are freely settable by whoever wrote the commit, so
they are recorded as evidence but never route on their own; without this a
pull-request author could pick the model that reviews their own change. Set
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
- repository pull-request list/detail, review list/detail, and current finding
  list;
- exact half-open review analytics with optional repository and author filters;
- commit-pinned hybrid code search; and
- evidence-bounded repository Q&A with claim-level source citations;
- an authoritative GitHub pull-request review trigger that re-fetches the
  current open head before enqueueing durable work;
- administrative GitHub repository registration that validates the exact
  configured origin and clone access before queueing the resolved
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

Diffuse reads per-repository settings from version-controlled `.diffuse/` files
in the repository under review, with deterministic root-to-leaf inheritance.
Defaults worth knowing before a first deployment: a pull request is reviewed
when it opens but **not** when further commits are pushed to it
(`triggers.review_updates`, default `false`), and no status check is published
(`triggers.status_check`, default `false`).

The complete reference — every file, field, default, and inheritance rule — is
in [docs/configuration.md](docs/configuration.md).

## Development

See [`DEVELOPMENT.md`](DEVELOPMENT.md) for the full environment setup, the
verified integration-test recipe, the migration rules, and the dependency
workflow. To report a vulnerability, see [`SECURITY.md`](SECURITY.md).

```bash
pip install -r requirements-dev.txt
pip install --no-deps -e .
pytest -m "not integration"
ruff check .
pip-audit -r requirements.lock --disable-pip
```

Integration tests need a disposable pgvector database — disposable because
`tests/integration/test_database_migrations_postgres.py` creates and drops whole
databases, so never point this at a database you care about. The full recipe,
including the health-check wait and the password substitution this summary
would gloss over, is in
[`DEVELOPMENT.md`](DEVELOPMENT.md#run-the-integration-tests).

If `POSTGRES_TEST_DATABASE_URL` is unset the suite reports **skipped**, not
failures — check for that before believing a green run.

Production images install the hash-locked
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
- persists normalized GitHub deliveries and exact pull-request revisions,
  deduplicates delivery retries and repeated revisions, preserves authoritative
  lifecycle events, and supersedes older queued heads;
- explicitly registers GitHub repositories and maintains credential-safe bare
  mirrors plus disposable exact-commit worktrees, with CLI and admin-authorized
  idempotent REST onboarding constrained to configured SCM origins;
- queues idempotent default-branch indexing from authenticated GitHub push
  events and serializes work for each host/repository ref;
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
- discovers tracked cascading `.diffuse` configuration, stable scoped rules,
  referenced context, and common
  agent/editor instruction files as immutable snapshot data, then enforces path
  ignores, pass selection, confidence/severity floors, review disablement, and
  summary-only mode before publication;
- normalizes bounded PR author/branch/label/title/draft and authoritative
  changed-file-count metadata, enforces
  cascading automatic trigger filters before retrieval, supports same-SHA
  ready/label transitions plus authorized `@diffuse` manual reruns, and
  records stable skip reasons without publishing noise;
- persists versioned review runs, findings, risk, 0–5 confidence, durable
  per-PR review numbers, token counts, immutable index and policy provenance,
  skipped decisions, and publication attempts;
- publishes a commit-pinned GitHub review with summary, issue table, confidence,
  severity, category, suggested fixes, exact LEFT/RIGHT inline comments, and a
  footer linking the reviewed commit with a working re-trigger command; policy
  can instead place the summary in a human-preserving managed PR-description
  region, suppress the visible summary, or hide fix guidance;
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
  foundation with pull-request list/detail, authoritative GitHub re-runs,
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
- optionally publishes a commit-pinned GitHub check with configurable blocking
  severities, exact-line status annotations, retry recovery, and deterministic
  success/failure/cancellation conclusions;
- conservatively auto-approves fully reviewed, clean changes only after
  strictest-wins path policy, hard critical-surface protections, inherent
  risk classification, exact-head revalidation, and durable idempotent
  publication;
- tracks durable finding lineages across commits, classifies new/persistent/
  addressed/reopened findings, avoids duplicate inline comments, and
  idempotently resolves or reopens the original GitHub review thread;
- accepts explicit authorized `@diffuse` questions on Diffuse-owned GitHub
  finding threads, serializes turns, retrieves snapshot-compatible repository
  context, filters unsupported model references, and publishes same-thread
  replies with durable retry/crash recovery;
- captures authorized finding-thread replies, periodically reconciles
  collaborator 👍/👎 reactions, records reaction withdrawals and commit
  outcomes, and exposes inspectable per-finding summaries with hard
  security/correctness/critical protection metadata;
- durably infers evidence-cited suggested rules after a configurable history
  threshold, consolidates duplicates, records immutable edit/approval/rejection/
  activation history, applies only active human-approved versions, and
  snapshots those versions onto review runs;
- recovers GitHub publication crash windows through hidden idempotency markers
  and managed description regions, suppresses self-induced description webhook
  loops, and falls back to complete summary details when GitHub rejects inline
  positions;
- uses parser-backed definition boundaries with bounded line-window fallback,
  without claiming compiler-grade type analysis;
- retrieves by cosine similarity from one embedding of the meaningful diff
  lines, while exact code identifiers remain recoverable through PostgreSQL
  full-text search;
- supports GitHub pull-request and default-branch push webhooks, review
  publication, native status output, grounded finding-thread replies, and
  authorized feedback ingestion.

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

Diffuse is licensed under the [Business Source License 1.1](LICENSE).
Copyright 2026 intuitumxyz.

**Proprietary and source-available, not open source.** BSL 1.1 is not approved
by the Open Source Initiative, so please do not describe Diffuse as open source.

- **You may** read and modify the source, run Diffuse in production, use it
  inside a commercial organization, process proprietary source code with it,
  and self-host it anywhere you control.
- **You may not** offer Diffuse to third parties as a hosted, managed, or
  embedded service, or otherwise sell access to Diffuse's functionality, before
  the applicable Change Date.

The current Licensed Work converts to the Apache License 2.0 on 2030-07-28, or
the fourth anniversary of its first public distribution, whichever comes
first. Later versions may carry different Change Dates.

Permitted self-hosted use requires no separate commercial agreement, license
key, or entitlement file. See
[ADR 0042](docs/adr/0042-source-available-self-hosted-distribution.md) for the
reasoning, and `legal@intuitum.xyz` for alternative licensing.
