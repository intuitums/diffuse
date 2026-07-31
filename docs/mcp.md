# MCP server

Diffuse serves a stateless JSON Streamable HTTP MCP endpoint at `/mcp`. It
gives an agent or IDE the same repository intelligence the review engine uses:
commit-pinned code search, citation-grounded repository Q&A, pull-request and
review state, durable review analytics, operator-managed custom context, and
revision-safe fix handoffs.

Everything it returns comes from durable PostgreSQL state — pull-request
records, review runs, published finding lineages, active immutable snapshots,
operator context, and inspectable feedback-derived rules — and every read and
write is constrained to the repositories assigned to the authenticated token.

The design decisions behind it are recorded in
[ADR 0021](adr/0021-authenticated-read-only-mcp-foundation.md),
[ADR 0023](adr/0023-durable-mcp-pr-lifecycle-and-custom-context.md),
[ADR 0024](adr/0024-descriptor-addressed-mcp-contract.md), and
[ADR 0025](adr/0025-revision-safe-agent-fix-handoffs.md).

## Authentication

`DIFFUSE_API_TOKEN` is a high-entropy bootstrap/recovery credential with
installation-wide access; set it to at least 32 visible ASCII characters and
keep it in the deployment secret manager. Routine clients should use durable,
least-privilege service tokens instead.

When the public host is not localhost, also set `DIFFUSE_PUBLIC_URL` to its
HTTP(S) origin and add the exact host (including its port when applicable) to
the comma-separated `DIFFUSE_MCP_ALLOWED_HOSTS` allowlist.

## Minting a service token

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
scope metadata but never credentials or hashes.

### Scopes

| Scope | Grants |
| --- | --- |
| `diffuse:mcp:read` | Repository, pull-request, review, finding, comment, and custom-context reads, plus plain source search |
| `diffuse:mcp:generate` | Model-backed repository Q&A |
| `diffuse:mcp:write` | Triggering reviews and creating, updating, or deleting custom context |

Add `--scope diffuse:mcp:generate` only for clients allowed to spend model
capacity on repository Q&A, and add `--scope diffuse:mcp:write` only for
clients allowed to trigger reviews or create custom context. Plain source
search requires only read scope; read-only credentials cannot invoke generation
or write tools.

## Connecting a client

For example, an MCP-compatible Codex client can use:

```bash
codex mcp add diffuse \
  --url https://diffuse.example.com/mcp \
  --bearer-token-env-var DIFFUSE_MCP_TOKEN
```

## Tools

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

The `merge_request` names predate GitHub-only support
([ADR 0041](adr/0041-github-only-source-control.md)) and describe GitHub pull
requests. `list_pull_requests` is the only one with a pull-request-named form;
`get_merge_request` and `list_merge_request_comments` have none. Renaming them
would break existing clients, so they stay as they are until a deliberate
contract change.

### Repository descriptors

The pull-request, review, and comment tools accept repository descriptors using
`name`, `remote`, `defaultBranch`, optional `remoteUrl`, and `prNumber`. Every
projected finding carries `diffuseGenerated`, which means the comment was
authored by the review system rather than a human. `list_repositories` exposes
the exact descriptors available to a token.

## Search and repository Q&A

`search_code` fuses literal identifier and one-hop graph evidence within an
optional literal path scope and returns immutable GitHub commit permalinks.

`ask_codebase` uses the same bounded evidence but emits claim-level citations;
claims with a missing, ambiguous, out-of-range, or unauthorized citation are
dropped, and no usable claims produces an explicit insufficient-evidence
response.

Optional repository-cluster search is intersected with the token's repository
grants. Model generation has its own scope, output/token limit, and timeout.

Repository Q&A inherits `REVIEW_MODEL` and `REVIEW_API_BASE`; set
`CODE_QUERY_MODEL` to choose a different LiteLLM model. Each call is bounded by
`CODE_QUERY_MAX_OUTPUT_TOKENS` and `CODE_QUERY_MODEL_TIMEOUT_SECONDS`. Plain
`search_code` never invokes the generation model: retrieval is graph and
lexical search inside PostgreSQL and needs no model credential.

## Triggering reviews

Review triggering fetches the current GitHub PR head before queuing work, and
closed/merged webhook state cancels queued reviews.

## Custom context

Active custom context is path-scoped, included in the review-policy fingerprint
and prompt, and snapshotted onto each review run.

Operator-created custom context can be edited or permanently deleted only with
MCP write scope and the exact `updatedAt` value most recently read. Conflicting
writers fail instead of overwriting one another; no-op retries preserve the
timestamp. Updates record field names, state/scope transitions, and
body/metadata hashes in the immutable audit log. Deletes retain a hashed audit
tombstone, while prior review runs keep the exact context snapshots they used.

Feedback-derived learned rules cannot be changed through these tools and retain
their evidence/approval/version workflow.

## Fix handoffs

Published GitHub reviews identify the exact MCP handoff call for each finding
and for Fix All. A handoff is available only while the review's base and head
still match an open PR and the finding remains the latest active lineage
occurrence.

It includes file/line/side, evidence, suggested fix, immutable review guidance,
SCM identity, and an agent-ready prompt that requires checkout verification,
minimal changes, relevant tests, and human control of commit/push. Supported
target labels are Codex, Claude Code, Conductor, Cursor, Devin, and generic
MCP; no agent receives credentials and the server never edits a developer
checkout.

## Review analytics

`get_review_analytics` accepts required timezone-aware `startAt` and `endAt`,
an optional repository descriptor, and an optional case-insensitive exact
`author` filter. Its window is half-open, bounded to 366 days, and always
normalized to UTC. Without a descriptor it aggregates only repositories
assigned to the token.

The report returns exact durable review attempts and status counts, distinct
PRs reviewed, opened PRs reviewed versus unreviewed, mean/median open-to-merge
time, lifecycle-timestamp completeness, completion and latency, token usage,
auto-approvals, custom-context adoption, applied finding occurrences and unique
lineages, current address/critical/security state, category/severity counts,
current upvote/downvote percentages and context-reply engagement, repository
breakdowns, UTC daily trends, and the twenty highest-priority open findings
with PR links.

Every rate includes its denominator definition, empty denominators return
`null`, and current-state metrics carry the report's database `asOf` time.
Diffuse refuses to substitute webhook receipt time when an SCM omits a
lifecycle timestamp and reports the resulting completeness explicitly.
Historical policy-eligible coverage and monetary cost remain unavailable until
their exact inputs are versioned.

See [ADR 0028](adr/0028-repository-authorized-review-analytics.md) and
[ADR 0029](adr/0029-authoritative-pull-request-lifecycle-analytics.md).

## Not yet implemented

Organizations/team RBAC and a local custom-URL bridge for literal one-click
agent launch are not yet implemented. Diffuse already marks findings addressed
or reopened from exact subsequent review diffs and projects GitHub thread state
through the comment tools.
