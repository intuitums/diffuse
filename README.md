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
  <a href="#install">Install</a> ·
  <a href="docs/">Documentation</a> ·
  <a href="DEVELOPMENT.md">Development</a>
</p>

---

AI can write code faster than teams can confidently review it. Diffuse closes
that gap with repository-aware review grounded in the exact code being changed
and the wider system around it.

Diffuse owns the review contract — policy, structured findings, lineage, and
publication — and the codebase index behind it. A self-hosted server reviews
pull requests on your infrastructure via a model API; local `diffuse review`
uses that same contract today and is built to rent a developer-installed agent
CLI (Claude Code or Codex) tomorrow. Graph and lexical retrieval, inspectable
team feedback, cross-repository context, and the same intelligence over CLI,
REST, and MCP stay under your control.

You run all of it inside infrastructure you control. There is no Diffuse-hosted
cloud control plane and no Diffuse account to create.

## Why Diffuse

| | |
| --- | --- |
| **Review with context** | Understands symbols, imports, calls, inheritance, and related repositories instead of reviewing an isolated diff. |
| **Find the signal** | Produces structured, schema-validated findings with evidence and fixes; the server path runs focused passes plus an independent verifier before publishing. |
| **Stay in control** | Keeps source-derived data in your environment unless you explicitly configure an external model, agent CLI, or integration. |
| **Improve with use** | Turns authorized replies and reactions into inspectable rule suggestions that require human approval before activation. |
| **Meet developers where they work** | Publishes native GitHub reviews and checks; the same index and policy are available through CLI, REST, and MCP. |

> [!NOTE]
> Diffuse is a working foundation under active development. See the
> [capability ledger](docs/capabilities.md) for what is implemented today, the
> [target architecture](docs/architecture.md) for where it is headed, and the
> [delivery roadmap](docs/roadmap.md) for the path between them.

## How it works

Two paths share one contract (`ReviewReport`, policy, index, publication rules).
[`docs/agent-runtimes.md`](docs/agent-runtimes.md) is the short reference.

**Self-hosted server** — pull requests from anyone who can open one:

```text
Repository onboarding CLI or versioned REST API
  ├─ registers an explicit GitHub repository
  ├─ derives a credential-free HTTPS clone URL
  ├─ maintains a locked bare mirror
  └─ queues the exact default-branch commit for indexing

GitHub push / PR / review-thread webhook and reaction ingestion
  └─ service.hosted.webhook_server
       ├─ verifies the webhook signature
       ├─ normalizes an exact commit or PR revision
       ├─ records authorized review replies as inspectable feedback
       └─ transactionally deduplicates and queues review/conversation work
            └─ service.hosted.worker
                 ├─ leases the durable job
                 ├─ fetches and indexes an exact commit in a disposable worktree
                 ├─ resolves exact primary + related-repository snapshots
                 ├─ retrieves graph + lexical context with provenance
                 ├─ runs the API review runtime (one-shot structured passes + verifier)
                 ├─ reconciles authorized 👍/👎 reactions on finding comments
                 ├─ infers evidence-cited rule suggestions for human moderation
                 ├─ applies only approved learned-rule versions
                 └─ publishes provider-native reviews and status idempotently
```

**Local CLI** — review the branch on your machine before or beside a PR:

```text
diffuse review
  ├─ identifies an enabled, indexed checkout from origin
  ├─ diffs the working tree against a selectable merge base
  ├─ resolves the same policy, learned rules, and snapshots
  └─ runs REVIEW_RUNTIME (today: the same API one-shot path;
       destination: optional claude / codex behind diffuse agent login)
```

The webhook acknowledges work only after its delivery and review job are
persisted. Workers use leases, bounded exponential retry, dead-letter state,
revision deduplication, and queued-job supersession.
[`docs/architecture.md`](docs/architecture.md) describes each stage in detail.

## Deployment model

Diffuse is self-hosted. You run the API, workers, PostgreSQL, repository
storage, and model connections in infrastructure you control. There is no
Diffuse-hosted control plane, no account to create, and no managed service.
Source-derived data does not leave your environment unless you explicitly
configure an external model or integration, and the model credentials Diffuse
uses are your own.

Permitted self-hosted use requires no separate commercial agreement, license
key, or registry credential. Diffuse contains no license-enforcement code. It
is proprietary and source-available, **not open source** — see
[Licensing](#licensing).

The digest-pinned Compose profile and signature-verification instructions live
in [`deploy/`](deploy/README.md); the root Compose file is the source-workspace
development profile. [`docs/deployment.md`](docs/deployment.md) is the
single-server runbook.

## Install

Requirements:

- Python 3.12+
- Docker with Compose
- a GitHub App installed on the target repositories, with its client/App ID,
  installation ID, and private key available to Diffuse
- a review-model API credential for the self-hosted worker (LiteLLM-compatible
  `REVIEW_MODEL`; see [Choosing a review model](#choosing-a-review-model)).
  Local agent-CLI review is planned and does not replace this for server/PR review.

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

Then point a GitHub webhook at `https://your-host.example/webhook/github`,
enabling exactly the `push`, `pull_request`, `issue_comment`, and
`pull_request_review_comment` events. Use the same random value for the
webhook's GitHub secret and `GITHUB_WEBHOOK_SECRET`; Diffuse refuses webhook
requests when the secret is missing or the signature is invalid.

Onboarding requires the exact SCM origin to match the configured primary origin
(`GITHUB_WEB_URL` / `GITHUB_API_URL`) or an exact entry in
`GITHUB_ALLOWED_INSTANCES`. Those hosts receive the resolved GitHub credential
during clone and fetch, so keep the list narrow: the allowlist is what stops an
API response from redirecting Git's askpass credentials to an arbitrary host.
Installation tokens are minted from the configured App private key, cached in
memory, and refreshed before their one-hour expiry. This profile still
configures one GitHub App installation per deployment; selecting installation
identity per tenant is future work.

Compose gates the API and worker on a one-shot migration service, so
`GET /ready` returns `200` only once the packaged migration history and
baseline database contract are current. One case needs an explicit decision: an
installation predating versioned migrations has application tables but no
migration ledger, and the migrator refuses to guess. After inspecting and
backing up that database, adopt it — every version-1 table and column is
verified first:

```bash
docker compose run --rm migrate database migrate \
  --adopt-existing \
  --actor operator@example.com
```

From here, [`docs/deployment.md`](docs/deployment.md) is the operational
runbook: backups, restore drills, upgrades, and why there is no rollback short
of a restore.

### Choosing a review model

`REVIEW_MODEL` is required and accepts LiteLLM model identifiers. There is no
built-in default: Diffuse will not assume you hold a credential for a provider
you never named, so an unset `REVIEW_MODEL` stops the worker at startup with an
error naming the variable rather than failing every pull request against a
provider you may not have an account with. `REVIEW_VERIFIER_MODEL` optionally
selects an independent verifier from another model family.

`REVIEW_DEPTH` sets how carefully to review — `brisk` through `exhaustive`. It
names an intent, not a provider parameter, because there is no single provider
parameter to name: Diffuse probes what the configured model supports and
decides what the intent becomes there, and a model that cannot express the
requested depth refuses to start rather than silently dropping it. Each review
records what it was actually asked to do in `review_depth_resolution`, so the
answer survives both `LOG_LEVEL` and a later configuration change. Configuring
a verifier from a second family additionally turns on authorship routing, which
runs an AI-authored change's candidate pass on the opposing family's model and
keeps the other as verifier — reordering the configured pair rather than
collapsing it.

Run `diffuse model` before onboarding the first repository. It reports the
provider, the credential variables it expects, readiness booleans without
printing secret values, and what each configured model actually supports — all
probed offline, so it answers before any credential exists.
`diffuse model --live` then makes one small schema-validated request against
the real endpoint. Two things it will tell you that are easy to miss: a
provider prefix Diffuse does not name explicitly (`mistral/…`, `groq/…`,
`xai/…`) is treated as a managed provider needing that provider's conventional
`<PROVIDER>_API_KEY`, and `REVIEW_STRUCTURED_OUTPUT_MODE=auto` falls back to
schema-constrained prompting with local Pydantic validation where a provider
has no native schema — invalid output fails the durable attempt and is never
posted to the SCM.

**Every variable is documented inline at its definition in
[`.env.example`](.env.example)** — cost guidance, token budgets, self-hosted
endpoints via `REVIEW_API_BASE`, and the exact prefix lists. Authorship routing
is specified in [`docs/architecture.md`](docs/architecture.md).

## The CLI

`diffuse` is the command-line interface to a self-hosted installation. It
manages repositories and cross-repository clusters (`repository`, `cluster`),
moderates feedback-derived rules (`learning`), mints scoped service tokens
(`token`), migrates and verifies the schema (`database`), inspects the
configured review model (`model`), signs in to agent CLIs Diffuse can host
(`agent`), scores an evaluation set (`evaluate`), and reviews the current local
branch (`review`) against the same index, policy, and learned rules as the
self-hosted server. Today local review still uses the API one-shot runtime;
agent-CLI selection is host plumbing only — see
[`docs/agent-runtimes.md`](docs/agent-runtimes.md).

```bash
diffuse review -b origin/main --diff
diffuse agent status
```

Install it with `uv pip install -e .`. The production image's `diffuse`
entrypoint additionally takes `serve`, `worker`, and `healthcheck`, which is how
Compose starts the API and worker.

Every flag, output mode, and the stable exit-code table scripts branch on are in
[`docs/cli.md`](docs/cli.md).

## Repository review policy

Diffuse reads per-repository settings from version-controlled `.diffuse/` files
in the repository under review, with deterministic root-to-leaf inheritance.
Defaults worth knowing before a first deployment: a pull request is reviewed
when it opens and again when further commits are pushed to it
(`triggers.review_updates`, default `true`, debounced by
`REVIEW_UPDATE_DEBOUNCE_SECONDS`), and no status check is published
(`triggers.status_check`, default `false`).

The complete reference — every file, field, default, and inheritance rule — is
in [docs/configuration.md](docs/configuration.md).

## MCP server and REST API

The same review context is available to agents, IDEs, and dashboards over two
authenticated surfaces, both using non-recoverable repository-scoped service
tokens minted by `diffuse token add`:

- **[MCP](docs/mcp.md)** — a stateless JSON Streamable HTTP endpoint at `/mcp`
  advertising twenty tools: commit-pinned code search, citation-grounded
  repository Q&A, pull-request and review state, review analytics, custom
  context, and revision-safe fix handoffs for Codex, Claude Code, Conductor,
  Cursor, and Devin.
- **[REST](docs/rest-api.md)** — a versioned control-plane API under `/api/v1`
  for repository/index state, PRs, reviews, findings, analytics, search, Q&A,
  and durably idempotent onboarding, reindex, and review triggers. It
  self-documents at `/docs` and `/openapi.json`.

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

Integration tests need a *disposable* PostgreSQL database, because
`tests/integration/test_database_migrations_postgres.py` creates and drops
whole databases — never point them at a database you care about. The full
recipe is in
[`DEVELOPMENT.md`](DEVELOPMENT.md#run-the-integration-tests). If
`POSTGRES_TEST_DATABASE_URL` is unset the suite reports **skipped**, not
failures; check for that before believing a green run.

To run the API directly while keeping Postgres in Docker:

```bash
set -a
source .env
set +a
uvicorn service.hosted.webhook_server:app --reload
```

### Review quality is not measured yet

`pytest` proves the review engine's plumbing, but every review test stubs the
model call, so the suite says nothing about whether the engine finds bugs.
`scripts/eval.sh` is the other half: it runs the real engine over the eight
labeled fixtures in `evals/fixtures/` and scores the result against a captured
baseline.

**No baseline is committed, so the gate is not live.** Capturing one requires
live model calls — roughly 40 per run — so `scripts/eval.sh` exits non-zero on
every machine rather than passing vacuously. Until it does, every confidence
threshold, severity floor, finding cap, and retrieval fusion weight in the
engine is a constant no run has confirmed or refuted, and no precision or
recall figure has been recorded from a real run. See
[`evals/CAPTURE.md`](evals/CAPTURE.md) for the capture procedure and
[`evals/README.md`](evals/README.md) for the scorer, the fixture format, and
its known limits.

## What works today

Diffuse is a working foundation, not a finished product. The honest status of
every capability lives in one place — **[the acceptance
ledger](docs/capabilities.md)** — rather than in a second list here that would
drift from it. It marks each capability `foundation`, `planned`, or `complete`,
and a capability is not `complete` merely because a prompt mentions it: it
needs a durable product path, tests, operator documentation, observability, and
a safe failure mode. Nothing is marked `complete` today.

Gaps worth knowing before you deploy: there is no web application, no
organizations or RBAC, and no usable browser sign-in — only repository-scoped
service tokens and the bootstrap credential authenticate a request. There is no
sandbox that executes pull-request code, no encrypted per-installation SCM
credentials, no metrics or traces, and no tenant isolation. Retrieval is
single-hop. Conversation answers questions on Diffuse-owned finding threads but
not top-level or arbitrary-line ones, and a top-level manual command triggers a
full review rather than a focused instruction. Chunk boundaries come from
parser-backed definitions with a bounded line-window fallback; this is not
compiler-grade type analysis.

[`docs/roadmap.md`](docs/roadmap.md) is the ordered path from here, including
which gates have to pass before the work that depends on them.

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
key, or entitlement file. Contact `legal@intuitum.xyz` for alternative
licensing.
