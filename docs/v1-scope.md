# Diffuse v1 scope

> **Status: active product decision.** This document is the source of truth for
> v1 scope. It supersedes conflicting feature claims and delivery sequencing in
> older roadmap, architecture, capability, and agent-operation documents until
> those documents are rewritten.

## Product promise

Diffuse is a self-hosted GitHub pull-request reviewer. It runs Codex and
Claude Code as isolated, read-only review investigators, verifies their
evidence, and publishes high-signal findings and a GitHub Check for the exact
pull-request head.

Diffuse does not promise to find every defect. Its promise is a grounded,
repeatable, low-noise review from multiple independent perspectives.

## v1 review flow

```text
GitHub PR head
  -> immutable checkout + deterministic context plan
  -> bounded, independent CLI investigations
       correctness | security | integration
  -> independent verification of candidate findings
  -> deterministic validation, deduplication, lineage, GitHub publication
```

Each investigation is read-only, has a strict time/cost budget, and returns a
structured candidate finding with code evidence and a failure mode. The
verifier can reject a candidate; only verified findings are published.

`standard` review is deliberately bounded. `deep` review is an opt-in,
higher-budget plan for large or high-risk changes, not an unbounded agent
swarm.

## Required v1 capabilities

- A guided, verified GitHub App connection. Setup creates or connects an
  operator-owned App, sends the operator through GitHub's installation screen,
  verifies the returned installation with that App's credentials, and confirms
  the repositories selected for review. It is one setup journey, not a
  Diffuse-user account or a generic GitHub OAuth login.
- GitHub App authentication, signed webhook ingestion, and idempotent
  publication of reviews and Checks.
- Repository mirroring, commit-pinned indexing, and targeted retrieval.
- Isolated Codex and Claude Code review runners. The worker never receives
  vendor credentials or executes either CLI.
- Structured candidate, verification, validation, finding-lineage, and
  re-review handling bound to the exact head SHA.
- One root repository configuration for enablement, ignored paths, minimum
  severity, draft handling, and review plan.
- Human feedback capture on published findings, plus explicit
  human-authored/approved repository guidance.
- A versioned, representative evaluation corpus that measures accepted issues,
  false positives, duplicates, latency, and cost before a review plan is
  broadly enabled.

## Explicitly removed from v1

- Public MCP server, MCP service tokens, MCP write tools, and agent handoffs.
- Automatic pull-request approval.
- A shared Intuitum GitHub App, multi-tenant webhook relay, and per-user
  Diffuse account system. Those are required for a literal hosted-SaaS
  "Connect with Diffuse" button and are a separate distribution decision.
- General public REST API as a product surface. Narrow private worker-to-runner
  transport is allowed.
- General repository Q&A, analytics reporting, cross-repository context, and
  agent-driven PR conversations.
- Arbitrary repository-code execution, shell access beyond the read-only review
  environment, branch mutation, and autonomous fixes.

## Explicitly deferred

- Automatic rule inference, automatic rule activation, and preference ranking.
- Nested/cascading repository policy, custom context, and organization policy.
- Advanced provenance-based model routing. It may return only after the
  baseline review team is measured.
- A full CLI-native replacement for every former LiteLLM operation. LiteLLM
  remains only while a CLI review path reaches the required quality and
  operational parity; it is not the v1 product identity.

## Terminology

Use product terms that describe the work performed:

| Retire | Use instead |
| --- | --- |
| agent runner | CLI review runner |
| agent session | review investigation |
| native runtime | CLI review engine |
| capability tool gateway | private review transport |
| `REVIEW_RUNTIME` | `REVIEW_ENGINE` |

The runner is an implementation detail. Diffuse is a review system, not a
general-purpose agent platform.

## Delivery order

1. **Documentation and work-plan reset.** Remove conflicting feature claims;
   align Linear with this scope.
2. **GitHub App connection.** Build the guided owner-App setup before review
   work: use the GitHub App manifest flow to create a correctly permissioned
   App when needed, secure the returned credentials, direct installation,
   verify the installation and selected repositories, then surface a clear
   ready/not-ready diagnostic. A public Diffuse endpoint is required for
   GitHub webhooks; local development uses an explicit webhook proxy.
3. **Excision.** Delete MCP and auto-approval code paths, configuration,
   tests, documentation, and public routes. Do not destructively drop their
   database data until an explicit migration decision.

## Deferred destructive migrations

Public MCP/REST/auto-approval runtime code is excised from v1, but the following
tables and columns remain in the schema pending a separate approved migration:

- `api_tokens`, `api_token_repositories`, `api_idempotency_keys`
- OAuth / session tables introduced in migration `0006`
- `review_auto_approvals`
- `review_runs.fix_with_agent_enabled`
- `custom_contexts.created_by_token_id`

Do not drop these in an opportunistic cleanup. Retaining them keeps existing
installations loadable while operators schedule an explicit data-removal
migration.

## Delivery order (continued)

4. **Review-runner contract.** Replace generic agent/session/capability naming
   with investigation/runner contracts; retain the isolation boundary.
5. **Single-engine end-to-end path.** Ship a complete Codex path first, with
   structured output, exact-head binding, verification, and GitHub
   publication; add Claude behind the same contract.
6. **Bounded review team.** Add the three specialist investigations and a
   cross-engine verifier. Publish only measured plans that improve accepted
   findings without unacceptable noise or cost.
7. **Feedback-backed learning.** Use feedback to make human-approved guidance
   useful and observable. Do not silently learn or suppress protected finding
   categories.
8. **LiteLLM retirement.** Decide only after the CLI review team has evaluation
   and pilot evidence.

## Documentation rule

Every user-facing capability document must distinguish **shipped**, **active
v1 work**, **deferred**, and **removed**. A planned component, table, route, or
security boundary must never be described in present tense.
