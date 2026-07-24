# Diffuse feature-parity contract

Last reviewed against Greptile's public product documentation: 2026-07-24.

## Objective

Diffuse must be a self-hostable code-intelligence and review platform with the
full user-visible capability set of Greptile. "Parity" means an operator can
achieve the same outcome inside their own infrastructure; it does not mean
copying Greptile's implementation, branding, UI, prompts, or undocumented APIs.

This document is the acceptance checklist. A capability is not complete merely
because a prompt mentions it: it needs a durable product path, tests, operator
documentation, observability, and a safe failure mode.

Status values:

- `foundation`: a limited precursor exists, but the parity outcome is not met.
- `planned`: no production-capable implementation exists yet.
- `complete`: acceptance criteria and end-to-end verification are satisfied.

## Parity matrix

| Area | Required Diffuse outcome | Status |
| --- | --- | --- |
| Repository onboarding | Connect and manage GitHub Cloud, GitHub Enterprise, GitLab Cloud, and self-managed GitLab; select repositories; auto-enable new repositories; support multiple code hosts at once. Admin/all-repository REST and operator CLI onboarding now constrain clone credentials to explicit origins, verify mirror access, and queue the exact default-branch commit; App/OAuth discovery, bulk selection, auto-enable, encrypted per-installation credentials, and UI remain. | foundation |
| Repository lifecycle | Secure clone/fetch, default-branch indexing, push-triggered incremental updates, deletion on revoked access, visible indexing state, retry/recovery, and large-monorepo support. GitHub and GitLab authenticated push indexing plus repository-authorized, idempotent REST reindexing reuse one exact-commit workflow; revocation deletion, operational UI, and monorepo evaluation remain. | foundation |
| Code graph | Parse mainstream languages into directories, files, symbols, variables, imports, calls, inheritance, usage, and dependency edges with stable identities across commits. | foundation |
| Semantic index | Embed code, summaries, docs, file paths, rules, and generated symbol descriptions; support hybrid lexical/vector/graph retrieval and model-aware re-indexing. | foundation |
| Impact analysis | Given a change, surface callers, callees, contracts, tests, imports, related patterns, and affected code across the repository. | foundation |
| Cross-repository context | Explicit related repositories, reusable repository clusters, access-control enforcement, and retrieval across shared libraries/SDKs. | foundation |
| Code search and repository Q&A | Search active immutable indexes by path, symbol, text, semantics, and graph relationships; answer general repository questions with verifiable commit-pinned source citations and fail closed when evidence is insufficient. MCP and REST search/Q&A, token-safe cluster context, separate generation scopes, and claim-level citation validation work; UI, multi-hop/type-aware retrieval, quotas, and evals remain. | foundation |
| Native review engine | Multi-turn agentic review grounded in diff, graph, repository, rules, memory, SCM metadata, and configured external context. PR-Agent must not remain a required runtime. | foundation |
| Review output | PR summary, risk score, confidence, issues table, inline comments, severity, category, concrete fixes, optional sequence diagrams, reviewed-commit marker, review count, re-trigger controls, managed PR/MR-description output, optional summary comments, and optional fix guidance. GitHub and GitLab have immutable publication controls, human-preserving description regions, self-webhook loop suppression, idempotent exact-line findings, complete enabled-summary fallback, authorized comment re-triggers, and repository-authorized MCP plus durably idempotent REST re-triggers; GitLab UI re-trigger controls remain. | foundation |
| Review quality controls | Strictness, category filters, file-change limits, ignored paths, model/turn selection, summary-only mode, status checks compatible with branch protection, and high-signal defaults. GitHub Checks and GitLab commit statuses share deterministic conclusions; exact-line status annotations remain GitHub-only. | foundation |
| Trigger policy | Open/ready-for-review/manual/update triggers plus label, author, branch, keyword, draft, file, and repository filters. GitLab open/update/lifecycle and Developer-authorized top-level manual commands use API-authoritative metadata; the provider-native ready-for-review action remains GitHub-only. | foundation |
| Security review | Dedicated security analysis, preventative-risk mode, evidence, security labeling, and security-focused evaluations. | foundation |
| Review conversation | Clarification questions and contextual replies in PR/MR threads; ignore acknowledgements and human-only conversation. Diffuse-owned GitHub and GitLab finding threads support authorized, ordered, grounded answers; top-level and arbitrary human threads remain. | foundation |
| Addressed detection | Determine whether findings were addressed across commits, resolve/update comments, and preserve review history. GitHub and GitLab finding threads resolve/reopen idempotently from one durable lineage. | foundation |
| Auto-approval | Conservatively approve clean, low-risk changes using configurable risk ceilings and strict author/branch/label/path/repository filters. GitHub and GitLab revalidate the exact head; GitLab also waits for approval/diff synchronization and pins the approval request to that SHA. Dashboard/org controls, scoped credentials, evals, and a kill switch remain. | foundation |
| Team memory | Learn from human PR comments, replies, reactions, accepted/rejected findings, and commit outcomes without suppressing security or correctness issues. Authorized GitHub/GitLab finding-thread replies, 👍/👎, withdrawals, and commit outcomes are durable; top-level feedback and preference ranking remain. | foundation |
| Suggested rules | Infer repeated team standards, deduplicate suggestions, and require an authorized human to approve/edit/reject learned rules. | foundation |
| Cascading configuration | Version-controlled `.diffuse/config.json`, `.diffuse/rules.md`, and `.diffuse/files.json` at any directory with deterministic root-to-leaf inheritance. A tracked root `greptile.json` is strictly imported when no native root policy exists; triggers, filters, ignored paths, context/rules/files, output sections, description/summary/fix publication settings, status checks, and an enforced strictness floor migrate. Unsupported ignore semantics and unknown fields fail explicitly. | foundation |
| Rule system | Structured rules with stable IDs, scope, severity, enable/disable, inherited-rule overrides, Markdown guidance, referenced files, and dashboard-managed org/team rules. | foundation |
| Existing instruction discovery | Detect and index files such as `AGENTS.md`, `CLAUDE.md`, Cursor rules, contribution guides, schemas, API specifications, and architecture docs. | foundation |
| Runtime validation | Generate targeted tests, execute the PR branch in an isolated sandbox, understand the repository stack, and attach reproducible evidence such as logs, traces, screenshots, scripts, and videos. | planned |
| Fix with an agent | Send one finding or all findings to Codex, Claude Code, Conductor, Cursor, Devin, and open MCP-compatible clients with file/line/context/fix data. Revision-safe MCP bundles and GitHub action instructions are working; the optional local one-click launch bridge remains. | foundation |
| CLI | Onboard, manage repositories, review a local branch against a base, resume, display inline output, and emit JSON or agent-friendly text against hosted or self-hosted Diffuse. | foundation |
| MCP server | Trigger/re-run reviews, inspect findings, search comments/code, answer repository questions, resolve findings through verified code updates, manage custom context/rules, and produce review/analytics reports. The documented PR/review/comment tool names and repository descriptor shapes work over repository-scoped durable state; code Q&A uses generation scope, authoritative GitHub/GitLab re-runs plus audited optimistic custom-context create/update/delete use write scope, and a Diffuse-native repository-authorized analytics tool exposes exact time-window reports. | foundation |
| Public API and webhooks | Versioned API for repositories, indexing, code queries, reviews, findings, rules, analytics, and integrations; signed outbound webhooks and stable idempotency semantics. `/api/v1` provides repository/index, PR, review, finding, analytics, hybrid-search, and grounded-Q&A reads using repository-scoped service tokens, OpenAPI, bounded inputs, separate generation authorization, and stable Problem Details. Review, admin onboarding, and repository-grant-scoped reindex mutations provide hashed actor/operation idempotency, conflict detection, crash-stable provider events, exact response replay, and audit; onboarding additionally enforces exact configured SCM origins and verified clone access. Authenticated GitHub/GitLab inbound webhooks use provider/host-scoped durable idempotency, strict size limits, GitLab replay protection, and exact instance allowlisting; lifecycle delete, rule/integration mutations, and signed outbound webhooks remain. | foundation |
| External context | Permissioned connectors for issue trackers, documentation systems, and partner-maintained API/SDK guidance with source attribution. | planned |
| Web application | Onboarding, repository/index status, review settings, rules/context, organizations/teams, members/roles, integrations, analytics, audit log, and operator settings. | planned |
| Organizations and RBAC | Organization/team hierarchy, inheritance and reset-to-default behavior, invitations, member/admin roles, repository scopes, and least-privilege authorization. | planned |
| Authentication | Local accounts, GitHub/GitLab OAuth, OIDC, SAML SSO, session management, service tokens, and optional SCIM provisioning. | foundation |
| Analytics | PRs reviewed, latency, merge time, addressed rate, severity/critical findings, reactions, review completion, cost/usage, filters, weekly reports, and export. The MCP foundation now reports exact authorized review/finding/engagement/token/context/approval metrics, author filtering, opened PRs reviewed versus unreviewed, authoritative mean/median merge time with completeness, repository and UTC daily breakdowns, reaction percentages, and linked open findings with explicit denominators. Team filters, historical policy-eligibility coverage, historical monetary pricing, UI, scheduled reports, and CSV/JSON export remain. | foundation |
| Audit and governance | Immutable actor/action/resource audit events, data-retention controls, model/provider policy, repository allowlists, usage limits, and administrative export. | planned |
| Self-hosting | Supported Docker Compose profile for small teams and Helm/Kubernetes profile for high availability and horizontal scaling. | foundation |
| Air-gapped operation | Offline images/artifacts, no required cloud control plane, local model and embedding endpoints, configurable SCM endpoints, and documented upgrade bundles. | planned |
| Model gateway | OpenAI-compatible providers, Anthropic, Bedrock, and local models; routing, retries, budgets, redaction, usage accounting, and model/embedding migrations. | foundation |
| Durable workflows | Persistent queues, idempotency, retries/backoff, cancellation, dead-letter handling, scheduling, concurrency control, and operator visibility. | foundation |
| Data services | PostgreSQL + pgvector, Redis-compatible cache, object/evidence storage, backup/restore, migrations, encryption, retention, and tenant isolation. The PostgreSQL foundation now has a frozen packaged baseline, append-only checksum-verified migration catalog, transaction-scoped concurrency lock, explicit contract-verified legacy adoption, CLI status/verification, and a Compose startup gate. Backups, rollback drills, cache/object storage, encryption, retention, and tenant isolation remain. | foundation |
| Observability | Structured logs, metrics, traces, health/readiness, queue depth, LLM latency/cost, indexing progress, review quality, alerts, and diagnostics bundles. | planned |
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
7. Diffuse is independently implemented from public behavior and open
   standards. Greptile-specific trademarks and private implementation details
   are out of scope.
8. Claims such as SOC 2, HIPAA, or GDPR compliance require an actual audit and
   operating program; code features alone do not justify them.

## Public reference surface

The checklist is derived from public Greptile documentation and changelog
entries, principally:

- <https://www.greptile.com/docs/code-review/key-features>
- <https://www.greptile.com/docs/code-review/first-pr-review>
- <https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context>
- <https://www.greptile.com/docs/how-greptile-works/memory-and-learning>
- <https://www.greptile.com/docs/code-review/greptile-config-reference>
- <https://www.greptile.com/docs/code-review/auto-approve-prs>
- <https://www.greptile.com/docs/code-review-bot/trigger-code-review>
- <https://www.greptile.com/docs/system-architecture>
- <https://www.greptile.com/docs/llms.txt>
- <https://www.greptile.com/changelog>
