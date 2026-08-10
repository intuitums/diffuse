---
name: pr-readiness
description: Audit the current branch against its base and return a decisive READY, READY WITH WARNINGS, or NOT READY verdict backed by blockers, warnings, and evidence. Works in any repository, language, stack, or forge by deriving every check from the repository itself, and delegates depth to Bugbot and Security Review rather than duplicating them. Not for line-by-line code review or security audits on their own — run /review-bugbot or /review-security directly instead. Use when asked "are we PR ready", "is this PR ready", "is this ready for a pull request", "can I open a PR", "ready to create/push/submit/raise a pull request", "ready to open a merge request", "is this ready for review", "is this ready to merge", "is this branch shippable", "should I open a PR yet", "PR check", or when asked to confirm a branch is ready to become a pull request, merge request, or change request.
---

# PR Readiness

Audit the current branch and decide whether it is ready to become a pull request. Report a single
decisive verdict with the evidence behind it.

This audit is read-only by default: do not commit, stage, push, create or update a request, rewrite
history, edit secrets or environment files, install dependencies, or fix findings until the user
explicitly approves. Running the repository's own read-only validation commands is expected and needs
no approval.

Never state that a check passed unless it actually ran or was verified this session. Every unrun
check belongs under "Checks skipped" with its reason.

Work the phases in order; report once, at the end. Do not narrate commands or repeat work that
already produced evidence this session.

## Portability doctrine

This skill assumes no language, framework, forge, CI system, or version control beyond the git
default.

- **Derive, never assume.** Every check comes from the repository in front of you; a command not
  documented, configured, or in CI is not its check — do not invent one.
- **Evidence ladder.** Stop at the first that answers: repository instructions, tooling
  configuration, CI definitions, convention for the ecosystems detected, then ask the user.
- **Vocabulary is interchangeable.** Pull request, merge request, and change request are the same
  audit; mirror the forge's terms.
- **Degrade, never fail.** Every external tool is optional; when one is missing, fall back and say so.
  Nothing may depend on tooling or accounts specific to one machine.
- **Multiple stacks coexist.** Detect every ecosystem the diff touches and validate each.

## Composition — delegate depth

This skill is a release gate, not a code reviewer: it owns scope, mechanical blockers, validation, CI
parity, and the verdict. Before Phase 3, delegate depth to Cursor's own reviewers. Never hand-roll a
deep diff review, and never issue a security verdict from regex scans.

- **`/review-bugbot`** — Bugbot correctness review, run against the branch locally before the diff
  leaves the machine. A local run and the later PR review share a patch ID, so pushing the same diff
  is not reviewed or billed twice.
- **`/review-security`** — the Security Review subagent.

Call the specific reviewer. **Do not call `/review`** — it only asks which of the two to run, which
stalls the audit on a question the verdict does not need. Both run against the session's working
directory, so confirm it sits inside the audited repo first. Never let a review write to files or
post to the request.

**A null result is not a pass.** "No diff to review", "not a git repository", or an empty answer
means nothing was reviewed; record it under "Checks skipped". Fold real results in with their source
(`from /review-bugbot`, `from /review-security`): critical and high findings become blockers, medium
and low warnings, once the range reviewed matches Phase 1's. If neither is usable, review inline to
Phase 3 depth and say so.
This skill's own scans stay a pre-filter for blockers a reviewer would not raise, not a substitute.

**Not this skill.** Reviewing code, finding bugs, or auditing security goes to those skills directly.

## Fast path

`scripts/pr-scan.sh`, beside this file, runs Phase 1's scope facts and Phase 3's mechanical scans in
one sub-second read-only pass, needing only git, awk, grep, and sed.

```
bash /path/to/pr-readiness/scripts/pr-scan.sh [base-ref]   # optional; it resolves one
```

Its output is **candidates, not findings**: open every hit in context, and treat absence of hits as
no evidence. It does not fetch, so heed its base-freshness warning. Nothing depends on it.

## Phase 1 — Establish scope

Detect the version control system first. The commands below are git; for anything else use its
equivalents and name it in the report — `references/edge-cases.md` covers the non-git cases.

Resolve the base in this order, and state which rule applied:

1. A base the user named explicitly.
2. An existing request for this branch, via whatever forge CLI this environment has. Skip silently
   when none is installed or authenticated.
3. The upstream tracking branch, then the remote default
   (`git symbolic-ref --short refs/remotes/<remote>/HEAD`).
4. The first existing trunk — `main`, `master`, `develop`, `trunk` — on the primary remote, then
   locally.
5. Ask when several trunks are plausible and nothing above disambiguates.

Forks, stacked branches, and release branches each move the base off trunk; `references/edge-cases.md`
carries the handling for each.

**Confirm the base is current before trusting any scope number.** Refresh with
`git fetch <remote> <base>` when the network allows; otherwise record the base as unverified. A stale
base silently inflates the diff with already-merged work, and the behind-count cannot reveal it. When
the range shows many commits, several authors, or subjects ending in `(#NNN)`, the base is wrong, not
the branch — re-resolve and re-run.

Audit both the change that would merge and the work that would be missing from it:

```
git status --porcelain=v1 -b                # branch, upstream, ahead/behind, dirty state
git merge-base HEAD <base>
git log --oneline <merge-base>..HEAD        # commits
git diff <merge-base>..HEAD                 # what would actually merge
git diff; git diff --cached                 # uncommitted work
git ls-files --others --exclude-standard    # untracked
```

Record those scope facts, how the base resolved, and commits behind base
(`git rev-list --count <merge-base>..<base>`). If the branch equals the base, has no
commits, or the base cannot be resolved, report that instead of guessing.

## Phase 2 — Profile the repository

Read the instructions that apply: `CLAUDE.md` and `AGENTS.md` (root and any nested ones covering
changed paths), `CONTRIBUTING.md`, `README.md` validation sections, and any request template. These
outrank this skill's defaults; cite them when a finding rests on one.

Then detect, from files present: ecosystems and package managers, layout, task runner, CI, migration
framework, and forge. `references/validation-discovery.md` lists the detection
signals. Scope the profile to what the diff touches — in a monorepo, audit the affected packages.

## Phase 3 — Inspect the change

Read the full diff, not just the stat — enough to judge scope, intent, and release risk, reading
surrounding code where ambiguous. Correctness and security depth is delegated per **Composition**;
do not re-derive it.

Then cover every scan class in `references/blocker-checks.md` — conflict markers, stray artifacts,
debug code, placeholders, secrets, unrelated changes, missing migrations, generated-output drift,
infrastructure risk, and documentation gaps. The fast path covers the mechanical ones. Scope scans to
changed files and prefer added lines, so pre-existing issues are not attributed here.

## Phase 4 — Validate

Discover the repository's real validation commands; `references/validation-discovery.md` carries
detection signals per ecosystem, CI parity, and safety rules.

- Prefer the commands the repository documents, then its task runner, scripts, or build file.
- Run only what covers the changed paths, scoped to affected packages in a monorepo.
- Use check or dry-run variants, never the writing variant. Never run anything that deploys,
  publishes, releases, pushes, mutates shared state, or requires credentials.
- Bound every command with an explicit timeout — `timeout 300 <cmd>`, or the tool's own timeout
  parameter. Skip what is expensive or unavailable, and record why.
- Capture the command, exit code, and a short excerpt of any failure.

Then read the repository's CI definitions, map each check CI will run to what ran locally, and
classify coverage as covered, partial, or not covered. With no CI, judge on local evidence and say so.

## Phase 5 — Judge intent and coverage

Infer the intended task from the branch name, commit messages, the diff, any linked issue, and the
conversation. Assess:

- Does the change accomplish that intent, and is anything obviously half-finished?
- Does the diff contain changes unrelated to that intent?
- Do tests exercise the changed behavior and its failure paths? Locate the tests touching the
  changed code rather than inferring coverage from a passing suite. A repository with no tests is
  following its own convention — say so rather than manufacturing a warning.

State the inferred intent explicitly so a wrong inference is visible.

## Phase 6 — Decide the verdict

Sort every finding into three kinds. The third keeps the verdict honest without charging the audit's
limits against the change.

- **Blocker** — breaks the build or CI, ships broken or unfinished behavior, leaks a secret, or
  commits an artifact that does not belong here. Includes failing tests, lint, type checks, or builds
  attributable to this change; conflict markers; committed credentials; a schema change with no
  migration; critical or high findings from a delegated review; and uncommitted or untracked work the
  change needs to function.
- **Warning** — a real defect the author should fix that does not disqualify the change: missing
  coverage for changed behavior, unrelated changes, documentation or changelog gaps, medium or low
  review findings, non-conforming commits.
- **Coverage note** — something this audit could not verify, through no fault of the change: a suite
  needing services, an unrun CI matrix cell, an uninstalled tool, a stale base. Report each under
  "Checks skipped" or "CI coverage", never as a pass — but do not charge it against the change.

Attribute a blocking failure before reporting it: re-run only the failing test or rule at the merge
base, never the whole suite. If even that is not cheap, mark the attribution unverified.

Apply the verdict strictly:

- **NOT READY** — one or more blockers.
- **READY WITH WARNINGS** — no blockers, at least one warning.
- **READY** — no blockers and no warnings. Coverage notes may still be present; list them so the
  reader sees exactly what was not checked.

Do not soften a verdict to be agreeable or manufacture findings to look thorough.

## Phase 7 — Report and follow through

Open with the verdict, then one line of scope facts (branch, base, commits, diffstat, tree state) and
the inferred intent. Then, in order: **Blockers**, **Warnings**, **Checks performed** (command and
exit status; delegated reviews with their source), **Checks skipped** (each with its reason), and
**CI coverage** (covered, partial, or not covered). Blockers and Warnings read `None.` when empty.
`references/report-format.md` holds the full contract and worked examples.

Keep findings concrete: cite `path:line`, state the consequence, and attach the output or diff hunk
that proves it. Order blockers by severity.

Then close with one of:

- **NOT READY** — a prioritized remediation plan, blockers first, each with its fix and rough cost.
  Offer to fix them; wait for approval before changing any file.
- **READY WITH WARNINGS** — the same plan for the warnings, plus the suggested title and description.
- **READY** — a title and description drawn from the actual diff, in the repository's own
  conventions, with a testing section listing only checks that genuinely ran. Fill the request
  template when one exists. Never open the request.

## Degraded conditions

`references/edge-cases.md` handles these in full — dirty trees, missing upstreams, forks, absent
tooling, unknown ecosystems, submodules, and scale; load it on hitting one. Three rules always hold:
never install anything or start services to make a check runnable, never stash or commit to tidy the
tree under audit, and when nothing is auditable, say so plainly and stop.
