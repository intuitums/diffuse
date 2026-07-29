# Diffuse capability ledger

## Objective

Diffuse must be a code-intelligence and review platform an operator can run
entirely inside infrastructure they control, at the capability level teams
expect from a hosted reviewer. Self-hosting is the architecture, not a
downgraded tier: every outcome below must be achievable without source code
leaving the operator's environment.

This document is the acceptance checklist and the honest status of each
capability. A capability is not complete merely because a prompt mentions it:
it needs a durable product path, tests, operator documentation, observability,
and a safe failure mode.

Status values:

- `foundation`: a limited precursor exists, but the required outcome is not met.
- `planned`: no production-capable implementation exists yet.
- `complete`: acceptance criteria and end-to-end verification are satisfied.

## Capability matrix

| Area | Required Diffuse outcome | Status |
| --- | --- | --- |
| Repository onboarding | Connect and manage GitHub Cloud and GitHub Enterprise; select repositories; auto-enable new repositories; support multiple GitHub instances at once. Admin/all-repository REST and operator CLI onboarding now constrain clone credentials to explicit origins, verify mirror access, and queue the exact default-branch commit; App/OAuth discovery, bulk selection, auto-enable, encrypted per-installation credentials, and UI remain. | foundation |
| Repository lifecycle | Secure clone/fetch, default-branch indexing, push-triggered incremental updates, deletion on revoked access, visible indexing state, retry/recovery, and large-monorepo support. GitHub authenticated push indexing plus repository-authorized, idempotent REST reindexing reuse one exact-commit workflow; revocation deletion, operational UI, and monorepo evaluation remain. | foundation |
| Code graph | Parse mainstream languages into directories, files, symbols, variables, imports, calls, inheritance, usage, and dependency edges with stable identities across commits. | foundation |
| Semantic index | Embed code, summaries, docs, file paths, rules, and generated symbol descriptions; support hybrid lexical/vector/graph retrieval and model-aware re-indexing. | foundation |
| Impact analysis | Given a change, surface callers, callees, contracts, tests, imports, related patterns, and affected code across the repository. | foundation |
| Cross-repository context | Explicit related repositories, reusable repository clusters, access-control enforcement, and retrieval across shared libraries/SDKs. | foundation |
| Code search and repository Q&A | Search active immutable indexes by path, symbol, text, semantics, and graph relationships; answer general repository questions with verifiable commit-pinned source citations and fail closed when evidence is insufficient. MCP and REST search/Q&A, token-safe cluster context, separate generation scopes, and claim-level citation validation work; UI, multi-hop/type-aware retrieval, quotas, and evals remain. | foundation |
| Native review engine | Multi-turn agentic review grounded in diff, graph, repository, rules, memory, SCM metadata, and configured external context. Deterministic commit-attribution analysis now routes high-confidence Anthropic/OpenAI-generated changes to an opposing model family without an LLM classification call, persists the evidence/model plan, and keeps ambiguous Cursor/Copilot or mixed provenance on cross-family review. | foundation |
| Review output | PR summary, risk score, confidence, issues table, inline comments, severity, category, concrete fixes, optional sequence diagrams, reviewed-commit marker, review count, re-trigger controls, managed PR-description output, optional summary comments, and optional fix guidance. GitHub has immutable publication controls, human-preserving description regions, self-webhook loop suppression, idempotent exact-line findings, complete enabled-summary fallback, authorized comment re-triggers, and repository-authorized MCP plus durably idempotent REST re-triggers. | foundation |
| Review quality controls | Strictness, category filters, file-change limits, ignored paths, model/turn selection, summary-only mode, status checks compatible with branch protection, and high-signal defaults. GitHub Checks apply deterministic conclusions with exact-line status annotations. | foundation |
| Trigger policy | Open/ready-for-review/manual/update triggers plus label, author, branch, keyword, draft, file, and repository filters. Open/update/lifecycle triggers evaluate the pull-request metadata carried in the signed webhook payload; authorized top-level manual commands re-fetch current state from the API before queueing. | foundation |
| Security review | Dedicated security analysis, preventative-risk mode, evidence, security labeling, and security-focused evaluations. | foundation |
| Review conversation | Clarification questions and contextual replies in PR threads; ignore acknowledgements and human-only conversation. Diffuse-owned GitHub finding threads support authorized, ordered, grounded answers; top-level and arbitrary human threads remain. | foundation |
| Addressed detection | Determine whether findings were addressed across commits, resolve/update comments, and preserve review history. GitHub finding threads resolve/reopen idempotently from one durable lineage. | foundation |
| Auto-approval | Conservatively approve clean, low-risk changes using configurable risk ceilings and strict author/branch/label/path/repository filters. GitHub publication revalidates the exact head. Dashboard/org controls, scoped credentials, evals, and a kill switch remain. | foundation |
| Team memory | Learn from human PR comments, replies, reactions, accepted/rejected findings, and commit outcomes without suppressing security or correctness issues. Authorized GitHub finding-thread replies, 👍/👎, withdrawals, and commit outcomes are durable; top-level feedback and preference ranking remain. | foundation |
| Suggested rules | Infer repeated team standards, deduplicate suggestions, and require an authorized human to approve/edit/reject learned rules. | foundation |
| Cascading configuration | Version-controlled `.diffuse/config.json`, `.diffuse/rules.md`, and `.diffuse/files.json` at any directory with deterministic root-to-leaf inheritance. Unknown fields and unsupported ignore semantics fail explicitly rather than being silently dropped. Importing third-party review configuration is not currently supported. | foundation |
| Rule system | Structured rules with stable IDs, scope, severity, enable/disable, inherited-rule overrides, Markdown guidance, referenced files, and dashboard-managed org/team rules. | foundation |
| Existing instruction discovery | Detect and index files such as `AGENTS.md`, `CLAUDE.md`, Cursor rules, contribution guides, schemas, API specifications, and architecture docs. | foundation |
| Runtime validation | Generate targeted tests, execute the PR branch in an isolated sandbox, understand the repository stack, and attach reproducible evidence such as logs, traces, screenshots, scripts, and videos. | planned |
| Fix with an agent | Send one finding or all findings to Codex, Claude Code, Conductor, Cursor, Devin, and open MCP-compatible clients with file/line/context/fix data. Revision-safe MCP bundles and GitHub action instructions are working; the optional local one-click launch bridge remains. | foundation |
| CLI | Onboard, manage repositories, review a local branch against a base, resume, display inline output, and emit JSON or agent-friendly text against hosted or self-hosted Diffuse. | foundation |
| MCP server | Trigger/re-run reviews, inspect findings, search comments/code, answer repository questions, resolve findings through verified code updates, manage custom context/rules, and produce review/analytics reports. The documented PR/review/comment tool names and repository descriptor shapes work over repository-scoped durable state; code Q&A uses generation scope, authoritative GitHub re-runs plus audited optimistic custom-context create/update/delete use write scope, and a Diffuse-native repository-authorized analytics tool exposes exact time-window reports. | foundation |
| Public API and webhooks | Versioned API for repositories, indexing, code queries, reviews, findings, rules, analytics, and integrations; signed outbound webhooks and stable idempotency semantics. `/api/v1` provides repository/index, PR, review, finding, analytics, hybrid-search, and grounded-Q&A reads using repository-scoped service tokens, OpenAPI, bounded inputs, separate generation authorization, and stable Problem Details. Review, admin onboarding, and repository-grant-scoped reindex mutations provide hashed actor/operation idempotency, conflict detection, crash-stable provider events, exact response replay, and audit; onboarding additionally enforces exact configured SCM origins and verified clone access. Authenticated GitHub inbound webhooks use provider/host-scoped durable idempotency, strict size limits, and exact instance allowlisting; lifecycle delete, rule/integration mutations, and signed outbound webhooks remain. | foundation |
| External context | Permissioned connectors for issue trackers, documentation systems, and partner-maintained API/SDK guidance with source attribution. | planned |
| Web application | Onboarding, repository/index status, review settings, rules/context, organizations/teams, members/roles, integrations, analytics, audit log, and operator settings. | planned |
| Organizations and RBAC | Organization/team hierarchy, inheritance and reset-to-default behavior, invitations, member/admin roles, repository scopes, and least-privilege authorization. | planned |
| Authentication | Local accounts, GitHub OAuth, OIDC, SAML SSO, session management, service tokens, and optional SCIM provisioning. Only repository-scoped service tokens and the bootstrap credential authenticate a request today. The GitHub OAuth endpoints (`/auth/cli`, `/auth/github/callback`, `/setup`) are mounted and mint a session row, but no `diffuse login` command exists and no authenticator reads a session, so browser sign-in cannot be completed end to end. There is no local account store and no OIDC, SAML, or SCIM implementation. | foundation |
| Analytics | PRs reviewed, latency, merge time, addressed rate, severity/critical findings, reactions, review completion, cost/usage, filters, weekly reports, and export. The MCP foundation now reports exact authorized review/finding/engagement/token/context/approval metrics, author filtering, opened PRs reviewed versus unreviewed, authoritative mean/median merge time with completeness, repository and UTC daily breakdowns, reaction percentages, and linked open findings with explicit denominators. Team filters, historical policy-eligibility coverage, historical monetary pricing, UI, scheduled reports, and CSV/JSON export remain. | foundation |
| Audit and governance | Immutable actor/action/resource audit events, data-retention controls, model/provider policy, repository allowlists, usage limits, and administrative export. An append-only `audit_events` table records actor kind/label, action, resource kind/id, optional repository, and a JSON detail object; seven production call sites write to it — repository onboarding and reindex, MCP review re-run, custom-context create/update/delete, and service-token issuance — and MCP context reads project the resulting history. Exact SCM origin allowlists are enforced at onboarding. Data-retention controls, model/provider policy, usage limits, a general audit query surface, and administrative export do not exist. | foundation |
| Self-hosting | Supported Docker Compose profile for small teams and Helm/Kubernetes profile for high availability and horizontal scaling. The Compose profile is supported; no Helm chart or Kubernetes manifest exists yet. | foundation |
| Air-gapped operation | Offline images/artifacts, no required cloud control plane, local model and embedding endpoints, configurable SCM endpoints, and documented upgrade bundles. | planned |
| Release distribution | Digest-pinned multi-architecture OCI images and install manifests, signed artifacts with provenance and SBOMs, supported update windows, and final runtime images without build-only material. The foundation packages one non-root executable, validates it in CI, publishes multi-architecture GHCR releases with SBOM/provenance attestations, signs the published digest with GitHub OIDC, emits a digest-pinned Compose bundle, and refuses to create a GitHub Release unless both OCI packages are anonymously pullable. Making the repository and package visibility public after [ADR 0042](adr/0042-source-available-self-hosted-distribution.md) lands, offline delivery, and support-window policy remain. No entitlement or license-enforcement mechanism exists, and per ADR 0042 none is planned. | foundation |
| Model gateway | OpenAI-compatible providers, Anthropic, Bedrock, and local models; routing, retries, budgets, redaction, usage accounting, and model/embedding migrations. Provider routing resolves credentials per family, including providers the gateway authenticates from the ambient environment, and `REVIEW_API_BASE` points review generation at a self-hosted OpenAI-compatible endpoint for unprefixed deployment names and self-hosted prefixes only. Bedrock routes and resolves the AWS credential chain, but `boto3` is not a runtime dependency, so Bedrock calls fail until it is installed. Embeddings take whatever `EMBEDDING_MODEL` names and pass it straight to LiteLLM, but the embedding path has no equivalent of `REVIEW_API_BASE` and resolves a credential only for OpenAI model names, so any other provider must be authenticated from the ambient environment and cannot be pointed at a self-hosted endpoint. The schema also pins vectors at 1536 dimensions with a `CHECK` constraint, so `EMBEDDING_DIMENSIONS` is configurable in name only. Budgets, redaction, and usage accounting remain. | foundation |
| Durable workflows | Persistent queues, idempotency, retries/backoff, cancellation, dead-letter handling, scheduling, concurrency control, and operator visibility. | foundation |
| Data services | PostgreSQL + pgvector, Redis-compatible cache, object/evidence storage, backup/restore, migrations, encryption, retention, and tenant isolation. The PostgreSQL foundation now has a frozen packaged baseline, append-only checksum-verified migration catalog, transaction-scoped concurrency lock, explicit contract-verified legacy adoption, CLI status/verification, and a Compose startup gate. Backups, rollback drills, cache/object storage, encryption, retention, and tenant isolation remain. | foundation |
| Observability | Structured logs, metrics, traces, health/readiness, queue depth, LLM latency/cost, indexing progress, review quality, alerts, and diagnostics bundles. Health and readiness are production-wired: `GET /health` is a liveness probe and `GET /ready` returns 200 only when the packaged migration history and baseline contract are current, which is what the runtime image's `HEALTHCHECK` polls and what the deployment and restore procedures gate on. Logs are plain `logging` records on the container's json-file driver. Metrics, traces, structured log records, queue-depth and indexing-progress surfaces, LLM latency/cost accounting, review-quality signals, alerting, and diagnostics bundles do not exist. | foundation |
| Security operations | Secret-manager integration, TLS, private networking, SSRF/egress controls, sandbox isolation, dependency/SBOM scanning, signed images, and documented threat model. | foundation |
| Quality evaluation | Versioned eval sets for correctness, security, noise, retrieval, cross-file impact, learning, and runtime validation with regression gates. | planned |

## Product principles

1. Self-hosted is the default architecture, not an enterprise bolt-on.
2. Source code, embeddings, review history, and feedback stay within the
   operator's chosen trust boundary.
3. Every review finding must be traceable to code, a rule, runtime evidence, or
   a clearly labeled model inference.
4. Graph, lexical, and semantic retrieval complement one another; embeddings
   alone are not "full codebase context."
5. Learned behavior is inspectable and reversible. Security and correctness
   findings cannot be silently trained away.
6. SCM permissions and tenant boundaries apply to every retrieval path,
   including cross-repository context.
7. Diffuse is independently implemented against open standards and provider
   APIs. Third-party trademarks, branding, prompts, and private implementation
   details are out of scope, and no *competitor* product name belongs in
   Diffuse's own contracts, identifiers, or documentation. Naming an
   integration target is different and is expected — the agent-handoff contract
   necessarily identifies the agents it targets.
8. Claims such as SOC 2, HIPAA, or GDPR compliance require an actual audit and
   operating program; code features alone do not justify them.
