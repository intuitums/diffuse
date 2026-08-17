# Diffuse v1 capability ledger

> **Status: active v1 scope.** This is the honest shipping ledger. Historical
> implementations and tests do not make a capability part of v1.

| Area | v1 outcome | Status |
| --- | --- | --- |
| GitHub integration | Signed GitHub App webhooks, exact-head review jobs, idempotent review and Check publication. | foundation |
| Repository context | Immutable mirrors, commit-pinned indexing, and targeted retrieval for a PR review. | foundation |
| CLI review runners | Isolated Codex and Claude Code runners with independent credential homes, read-only workspaces, private context access, and bounded egress. | foundation |
| Review investigation | Exact-head candidate and independent verifier investigations use signed role, budget, workspace, and result contracts. Specialist correctness/security/integration roles remain planned. | foundation |
| Verification and publication | An independent verifier plus deterministic validation and deduplication decides what reaches GitHub. | foundation |
| Review modes | A measured `standard` plan and an opt-in bounded `deep` plan, each with explicit time/cost limits. | planned |
| Root review configuration | One small root configuration for enablement, ignored paths, severity floor, draft behavior, and review plan. | foundation |
| Feedback and guidance | Durable feedback plus explicit human-authored or human-approved guidance; protected correctness/security findings cannot be silently suppressed. | foundation |
| Quality evaluation | A versioned corpus measuring accepted findings, false positives, duplicates, latency, and cost before broad enablement. | planned |
| Self-hosted operation | Compose-based deployment, PostgreSQL workflow state, backups, and operator diagnostics. | foundation |

## Removed from v1

- Public MCP, service tokens, MCP write tools, agent handoffs, and general REST
  API product surfaces.
- Automatic pull-request approval.
- General repository Q&A, analytics reports, cross-repository context, and
  agent-driven PR conversations.
- Nested policy, automatic rule inference/activation, and generic preference
  learning.
- Autonomous fixes, branch mutation, and arbitrary repository-code execution.

## Deferred

Feedback-backed, human-approved guidance is deliberately after the review path
and quality pilot. A future decision may consider per-finding dismissal or safe
down-ranking from repeated feedback, but not automatic suppression of protected
categories.

Direct model API execution is retired. Reviews run through the isolated Agent
Host path.

## Evidence rule

“Foundation” means a useful implementation seam exists; it does not mean the
v1 outcome is shipped. “Planned” means no end-to-end production path exists.
No document, test, or code comment may claim a deferred or removed feature as a
current Diffuse capability.
