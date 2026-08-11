<p align="center">
  <img src="assets/logo/mark-black-on-acid-rounded.svg" width="120" height="120" alt="Diffuse">
</p>

<h1 align="center">Diffuse</h1>

<p align="center">
  <strong>Self-hosted GitHub pull-request review, powered by isolated Codex and Claude Code CLI investigators.</strong>
</p>

Diffuse is being deliberately narrowed for v1. It receives a GitHub pull
request, pins the exact head and repository context, asks bounded read-only CLI
review investigators to look for correctness, security, and integration
problems, verifies their evidence, and publishes only high-signal findings and
a GitHub Check.

This is a review product, not a general-purpose agent platform. It does not
promise to find every defect, auto-approve pull requests, answer arbitrary
repository questions, run an MCP service, or mutate a branch.

> **Status:** the repository contains transitional implementation from the
> earlier product direction. The active v1 decision and removal order are in
> [docs/v1-scope.md](docs/v1-scope.md). Do not rely on older feature claims in
> historical documents or code comments as a shipping commitment.

## The v1 review flow

```text
GitHub pull-request head
  -> immutable checkout + deterministic context plan
  -> bounded CLI investigations
       correctness | security | integration
  -> independent verification
  -> deterministic validation, deduplication, lineage, GitHub publication
```

Every investigation is read-only, exact-head-bound, time/cost-limited, and
returns structured evidence. Diffuse alone validates and publishes. `standard`
is the bounded default; `deep` is a measured, opt-in higher-budget plan—not an
unbounded swarm.

## What v1 includes

- One guided, verified GitHub App connection: create or connect an
  operator-owned App, install it through GitHub, verify the installation, then
  choose the repositories to review. Signed webhooks publish idempotent reviews
  and Checks.
- Commit-pinned repository mirrors, indexing, and targeted retrieval.
- Isolated Codex and Claude Code CLI review runners. The worker never executes
  either CLI or receives vendor credentials.
- Structured investigator candidates, independent verification, exact-head
  lineage, and re-review handling.
- One small root repository configuration and human feedback capture.
- Durable report and tool-call records needed for future Agent evaluation.

## Explicit non-goals for v1

- Public MCP, MCP service tokens, agent handoffs, and a general public REST API.
- Automatic pull-request approval.
- General repository Q&A, analytics, cross-repository context, or PR
  conversations.
- Automatic policy inference or activation, autonomous fixes, or arbitrary
  repository-code execution.

## Documentation

Read [the v1 scope](docs/v1-scope.md) first. The current reference map is in
[docs/README.md](docs/README.md).

For local development — setup, tests, lint, and the database migration rules —
use [AGENTS.md](AGENTS.md). It documents verified commands; it is not a
promise that every currently exposed endpoint or configuration field survives
the v1 reset.

## Delivery order

1. Remove stale documentation and plans.
2. Build the one-flow, verified GitHub App setup and repository connection.
3. Excise public MCP and automatic approval without initially dropping stored
   data.
4. Finish the private CLI review-runner contract.
5. Ship one end-to-end CLI review engine, then the second.
6. Add the bounded review team only when evaluation shows it improves quality.

See [docs/v1-scope.md](docs/v1-scope.md) for the complete decision record.
