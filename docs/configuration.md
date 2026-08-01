# Repository review policy

Diffuse reads its per-repository configuration from version-controlled files in
the repository under review. This is the complete reference for those files:
what they may contain, how directory-level files inherit and override one
another, and what each setting does.

Everything here is optional. A repository with no `.diffuse/` directory is
reviewed with the defaults described below.

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
    "failure_comment": true,
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
      "allow_paths": ["docs/**", "src/ui/**"],
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

Automatic reviews run by default for newly opened and reopened PRs and for new
commits pushed to an open one. Draft PRs are skipped unless `review_drafts` is
enabled, and `review_updates: false` turns the re-review on push back off.
Because a push costs a model call, a pushed revision is held for
`REVIEW_UPDATE_DEBOUNCE_SECONDS` (a deployment setting, default `60`) before the
worker may claim it, so a burst of pushes produces one review of the final head
rather than one per commit; the wait is measured from the first push of the
burst. Ready-for-review, label, keyword-edit, and relevant label-removal
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
retrieval or review-model call.

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

`auto_approval.enabled` defaults to `false`, and so does every path: approving
is a write action, so `auto_approval.filters.allow_paths` is the list of places
where you permit it. A repository that enables auto-approval without naming any
path approves nothing, and so does one that names paths the pull request does
not touch. Diffuse deliberately does not infer this list — no denylist it ships
can know whether your authorization code lives in `src/auth/`, `internal/perms/`,
or `pkg/tenancy/`, and the paths it fails to name are the ones that would be
approved by accident. An empty `allow_paths` list is a decision rather than an
omission: it withdraws a grant a parent scope made.

Keeping the allowlist in the repository is safe because Diffuse resolves it from
the indexed default-branch snapshot rather than from the pull request's head
commit. A pull request that edits `.diffuse/config.json` is reviewed under the
configuration that was already merged, so it cannot allowlist the paths it needs
approved in the same change; the new allowlist takes effect after it lands and
the repository is reindexed. Those edits are also critical-risk in their own
right, so the pull request making them is never a candidate for approval.

When requested, Diffuse records an inspectable decision after the ordinary
review and status check finish, and submits a commit-pinned GitHub review only
when all of these hold:

- every touched path scope enables auto-approval;
- every changed path is named by the allowlist of every scope that governs it;
- authoritative PR metadata and the diff are complete, including both sides of
  renames;
- all configured author, target-branch, label, keyword, repository, path, and
  file-count filters pass;
- every changed file was reviewed, no path was ignored, the current report has
  no findings, no earlier finding lineage remains open, and risk score is zero;
- Diffuse's deterministic change-risk class does not exceed the strictest
  configured ceiling.

Low covers documentation, styling, and very small changes; ordinary application
changes are medium; dependency/build/runtime/shared-core changes are high. Test
paths are never low, however small the diff: a test file carries the evidence
that production behaviour is correct, and removing one assertion is a one-line
change that makes nothing fail, so a test-only pull request needs a
`risk_ceiling` of at least `medium` and an allowlist that names it. Auth, public
API, secret, billing/payment, schema/migration, CI, and infrastructure paths are
critical and are never automatically approved, including when `risk_ceiling` is
set to `critical` and including when `allow_paths` names them — the built-in
critical list is a floor under the allowlist, not an alternative to it, which is
what keeps a pull request from allowlisting the configuration that approves it.
Nested scopes merge strictest-wins: every scope must enable the feature, every
scope's allowlist must cover the path (nested patterns are rooted at their own
scope, so a nested `**` cannot widen a parent's grant), the lowest ceiling and
smallest file limit win, exclusion filters union, and every applicable inclusion
filter must match. Approval state and attempts are durable. Before
posting, Diffuse re-fetches the pull request and cancels approval if it closed,
became a draft, or moved to another head commit. A hidden per-run/head marker
recovers remote-create crash windows.

`context.repos` also replaces its inherited value for the applicable path.
Every entry must be an explicitly onboarded, enabled repository on the exact
same GitHub host as the reviewed repository. An explicit repository
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

Cluster members must share one GitHub host. Explicit entries take
precedence, cluster membership adds deduplicated repositories, and the combined
plan is capped at seven related repositories. Disabled or not-yet-indexed
cluster members are skipped; explicit entries fail closed so a committed
dependency cannot disappear silently. Related repositories contribute
read-only lexical reference chunks. They do not create synthetic
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

`failure_comment` defaults to `true`, because a failed review must never be
silent. When a review job fails terminally — retries exhausted, or an error
that retrying cannot resolve — Diffuse posts one comment on the pull request
naming the error-code slug and the workflow job id so an operator can find the
failure in the worker logs. The notice carries no exception text,
traceback, or credential, and credential-shaped substrings are redacted before
publication. Posting is best effort: it can never mask the original failure.
The comment carries a Diffuse-owned `diffuse-review-failure` marker, which both
makes a replayed terminal path reuse the existing comment instead of posting a
second one and keeps Diffuse from re-ingesting its own notice as human
feedback. Because a review can die before its diff is ever read, only the
repository-root `.diffuse` layer governs `failure_comment`; nested scopes
cannot disable it. Set it to `false` at the repository root to opt out.

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
of the GitHub pull-request description while preserving all human-authored
text. The region is replaced idempotently on retries and is written only after
GitHub confirms that the pull request is still open at the reviewed head.
`summary_comment=false` suppresses the visible top-level review summary without
suppressing eligible exact-line findings, and `fix_with_agent=false` hides
published fix-one/fix-all guidance and suggested-fix blocks without deleting
the durable finding evidence. Description output takes precedence over the
summary-comment setting. Diffuse ignores the GitHub webhook generated solely by
its managed-region write, but still reviews genuine human description edits.

When reviews run on later commits, Diffuse compares the previously published
head with the new head and maintains finding lineage across line movement and
minor wording changes. A finding remains open when it is detected again. When
a pushed commit touches its file and the verifier no longer detects it, Diffuse
posts one idempotent addressed reply and resolves the original GitHub thread.
If the finding returns, Diffuse reopens that same thread and records the
transition. Only genuinely new lineages create new inline comments; persistent
findings do not create duplicates.

Lineage changes are provisional until the commit-pinned review is durably
published. Superseded or terminally failed reviews discard their provisional
events. An enabled status check considers every active lineage, so an unresolved
blocking finding from an earlier review cannot silently pass because a later
model call omitted it.

An authorized repository member can deliberately bypass all automatic trigger
filters—including draft and update gates—by starting a top-level pull-request
comment line with `@diffuse`. GitHub requires a human owner/member/
collaborator. Diffuse fetches fresh metadata for the open pull request from the
GitHub API, and the comment ID gives every manual rerun a distinct idempotency
identity. Manual questions in a top-level comment still start a complete
review.

On an inline finding created by Diffuse, an authorized repository member can
reply with `@diffuse <question>` to ask for clarification, alternatives,
testing guidance, or related repository patterns. Diffuse verifies that the
root is one of its stored finding threads and checks GitHub owner/member/
collaborator authority. It then retrieves hybrid graph/lexical context
around the finding, includes prior published turns, and posts a structured
answer with only validated code references into the same GitHub thread.
Questions on the same thread
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
👍 and 👎 reactions from GitHub collaborators become positive or negative
signals; other emoji and unauthorized actors are neutral, removed reactions are
recorded as withdrawn, and addressed/reopened commit outcomes are retained
separately.

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

