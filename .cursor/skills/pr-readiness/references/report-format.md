# Report format

The full output contract for Phase 7. Emit the report once, at the end of the audit. Keep it
scannable: no preamble, no restating the phases, no narration of commands already listed under
"Checks performed".

The examples below happen to be Node, Rust, and Terraform. The format is stack-agnostic — the same
sections apply to any language, forge, or VCS, with commands and terminology swapped for whatever the
repository actually uses.

## Rules

- Lead with the verdict. It is the first thing on the page.
- Mirror the repository's vocabulary: pull request on GitHub, merge request on GitLab, change on
  Gerrit. Retitle the report heading to match rather than forcing "PR".
- Name the checks in the repository's own terms — its script names, targets, or CI job names — so the
  reader recognizes them.
- One line of scope facts so the reader knows exactly what was audited.
- State the inferred intent explicitly — a wrong inference must be visible.
- Every finding cites `path:line` and states the consequence, not just the symptom.
- Every claim of a passing check names the command that produced it.
- Omit empty sections except "Blockers" and "Warnings", which read `None.` when empty so the reader
  knows they were considered.
- Keep coverage notes out of "Warnings". Something the audit could not verify — a suite needing
  services, an unrun matrix cell, a missing tool — belongs under "Checks skipped" or "CI coverage".
  A READY verdict may carry coverage notes; it may not carry warnings.
- Never print secret values. Cite the location and the credential type.
- Sort blockers by severity, warnings by impact.

## Worked example — NOT READY

```
## PR Readiness: NOT READY

Branch feat/session-expiry -> origin/main | 6 commits | 14 files, +512/-88 | tree dirty
Base resolved from origin/HEAD; branch is 3 commits behind main.
Intent: expire idle sessions after 30 minutes and surface a re-auth prompt.

### Blockers
1. Unresolved conflict markers shipped in source — src/auth/session.ts:142
   Conflict markers from the merge of main remain in the file. The module fails to parse, so
   every importing test fails. Evidence: `npm run typecheck` exits 2 with
   "TS1185: Merge conflict marker encountered" at session.ts:142.
2. Type check fails on the new expiry helper — src/auth/expiry.ts:31
   `lastSeenAt` is `Date | null` but is passed to `differenceInMinutes` unguarded.
   Evidence: `npm run typecheck` — TS2345, exit 2.
3. Session-schema change ships without a migration — prisma/schema.prisma:88
   `Session.expiresAt` was added as a required column with no new directory under
   prisma/migrations/. Deploying this fails on an existing database.
4. Required change is uncommitted — src/middleware/auth.ts
   The middleware wiring that calls the new expiry check exists only in the working tree, so the
   PR as pushed would contain a dead helper. Evidence: `git status --porcelain` shows ` M`.

### Warnings
- No test covers the expiry boundary — src/auth/expiry.ts:24
  Tests exercise the expired and fresh cases but not the exact 30-minute edge.
- Unrelated dependency bump — package.json:41
  `date-fns` 2.30 to 3.6 is a major bump unrelated to the stated intent; consider a separate PR.
- Branch is 3 commits behind origin/main; the merge result was not validated.
- Commit 4a2f1c9 subject is "wip fix" — repository CONTRIBUTING.md requires conventional commits.

### Checks performed
- `npm run typecheck` — FAIL (exit 2), 3 errors
- `npm run lint` — PASS (exit 0)
- `npm test -- src/auth` — FAIL (exit 1), 4 failing in session.test.ts
- `npx prettier --check src` — PASS (exit 0)
- `git grep -nE '^(<{7}|={7}|>{7})'` on changed files — 1 hit
- Secret scan over added lines — no credible hits

### Checks skipped
- `npm run test:e2e` — requires a running Postgres and Playwright browsers; not started.
- `npm run build` — not run after typecheck already failed; would be redundant.
- Coverage thresholds — not evaluated locally.

### CI coverage
| CI check | Local equivalent | Coverage |
| --- | --- | --- |
| lint | `npm run lint` | covered |
| typecheck | `npm run typecheck` | covered |
| unit (node 18, 20 matrix) | `npm test -- src/auth` on node 20 | partial — scoped, node 18 not run |
| e2e | none | not covered |
| migrate --check | none | not covered — and blocker 3 makes failure likely |

### Remediation plan
1. Resolve the conflict in session.ts:142 and re-run typecheck. (blocker 1, minutes)
2. Guard the null `lastSeenAt` in expiry.ts:31. (blocker 2, minutes)
3. Generate the Prisma migration for `Session.expiresAt`, with a default or a backfill for existing
   rows. (blocker 3, ~30 minutes — needs a decision on the default)
4. Commit the middleware wiring in src/middleware/auth.ts. (blocker 4, minutes)
5. Add the 30-minute boundary test. (warning)
6. Split the date-fns major bump into its own PR. (warning)
7. Rebase onto origin/main and re-run the suite. (warning)

Want me to fix blockers 1, 2, and 4? Blocker 3 needs your call on the default value for existing
sessions. Nothing has been changed so far.
```

## Worked example — READY

```
## PR Readiness: READY

Branch fix/rate-limit-headers -> origin/main | 2 commits | 4 files, +86/-12 | tree clean
Base up to date; branch is level with origin/main.
Intent: return standard RateLimit-* headers on throttled responses.

### Blockers
None.

### Warnings
None.

### Checks performed
- `cargo check` — PASS (exit 0)
- `cargo clippy -- -D warnings` — PASS (exit 0)
- `cargo test --package api` — PASS (exit 0), 84 tests
- `cargo fmt --check` — PASS (exit 0)
- `/review-bugbot` — no findings
- `/review-security` — no findings on the changed paths
- Diff review of all 4 files, plus the two call sites of `apply_limits`
- Scans for conflict markers, debug code, placeholders, artifacts, and secrets — no hits

### CI coverage
| CI check | Local equivalent | Coverage |
| --- | --- | --- |
| fmt | `cargo fmt --check` | covered |
| clippy | `cargo clippy -- -D warnings` | covered |
| test | `cargo test --package api` | covered — CI runs the full workspace; unchanged crates unaffected |

### Suggested PR

Title: fix(api): return standard RateLimit headers on throttled responses

Body:
## Summary
Throttled responses returned only `Retry-After`, so clients could not see their remaining quota.
This adds `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` to every 429 response and
to successful responses passing through the limiter.

## Implementation notes
- Header assembly lives in `apply_limits` so both the middleware and the manual guard share it.
- `RateLimit-Reset` is emitted in seconds, matching the IETF draft, rather than a timestamp.

## Testing
- `cargo test --package api` (84 tests)
- `cargo clippy -- -D warnings`
- `cargo fmt --check`

Ready to open. Say the word and I will create the PR.
```

## Worked example — READY WITH WARNINGS, non-mainstream stack

Demonstrates a GitLab merge request, an infrastructure repository, a partially discoverable
toolchain, and honest reporting of what could not be verified.

```
## Merge Request Readiness: READY WITH WARNINGS

Branch infra/rds-multi-az -> origin/main | 3 commits | 5 files, +74/-19 | tree clean
Base resolved from origin/HEAD (glab not installed; no existing MR metadata).
Intent: enable multi-AZ on the production RDS instance and widen the backup window.

### Blockers
None.

### Warnings
- Instance class change forces replacement — terraform/rds.tf:47
  `instance_class` moves from db.t3.medium to db.m6g.large alongside multi_az. On this provider
  version that is an in-place modify, but combined with the engine bump it may force a replacement.
  A plan against real state is required before merge; it could not be run here.
- Backup retention lowered from 30 to 7 days — terraform/rds.tf:52
  Likely unintentional given the stated intent, and may breach the retention policy in docs/ops.md.
- No changelog entry — the repository keeps CHANGELOG.md and this is an operator-visible change.

### Checks performed
- `terraform fmt -check -recursive` — PASS (exit 0)
- `terraform validate` — PASS (exit 0), using the committed .terraform.lock.hcl
- `tflint --recursive` — PASS (exit 0)
- Diff review of all 5 files, plus the module inputs in terraform/modules/db/
- Scans for conflict markers, artifacts, placeholders, and secrets — no hits
- Reviewed for destructive resource changes — one candidate, reported above

### Checks skipped
- `/review-bugbot` — not available in this environment; the diff was reviewed inline instead.
- `terraform plan` — requires AWS credentials and reads live state; not run.
- `checkov` — not installed locally; not installed for this audit.
- `terraform test` — the repository defines none.

### CI coverage
| CI job (.gitlab-ci.yml) | Local equivalent | Coverage |
| --- | --- | --- |
| fmt | `terraform fmt -check -recursive` | covered |
| validate | `terraform validate` | covered |
| tflint | `tflint --recursive` | covered |
| checkov | none | not covered — policy findings unknown |
| plan (manual, prod) | none | not covered — the decisive check for this change |

### Remediation plan
1. Confirm the backup retention drop is intended, or restore 30 days. (warning, minutes)
2. Run the pipeline's manual plan job and confirm no replacement of the RDS instance before merge.
   (warning — this is the check that actually de-risks the change)
3. Add the CHANGELOG entry. (warning, minutes)

### Suggested MR

Title: feat(infra): enable multi-AZ and resize production RDS

Body:
## Summary
Enables multi-AZ failover on the production RDS instance and moves it to db.m6g.large. Also adjusts
the backup window to fall outside the nightly ETL.

## Risk
Instance resize may require a maintenance window. The pipeline's plan job must be reviewed before
merge; retention and replacement behavior are unverified locally.

## Testing
- `terraform fmt -check -recursive`, `terraform validate`, `tflint --recursive`
- No plan or apply was run locally.

Open the MR when you are ready — I have changed nothing. Want me to fix the retention value and add
the changelog entry?
```

## Verdict phrasing

Do not hedge the verdict line and do not invent a fourth category. When evidence is genuinely
insufficient to judge — the base is unresolvable, the repository is not a git repository, the diff
is empty — report that condition plainly in place of a verdict rather than defaulting to READY.
