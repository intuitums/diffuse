---
description: Audit whether current changes are ready to open or merge
argument-hint: "[base branch]"
---
Audit the current repository for PR readiness. The optional base ref supplied to
this command is `${1:-<not supplied>}`. Treat `<not supplied>` as a request to
detect the base automatically; otherwise require the value to resolve to a valid
Git ref. This is a read-only release gate: do not edit, stage, commit, push,
rewrite history, modify a PR, install dependencies, update snapshots, apply
migrations, publish, or deploy.

Remote-ref fetches are allowed when needed to establish a current base. Existing
repository checks may create ignored caches; record status before and after and
do not clean them up without permission. Never expose credential values or
claim an unrun check passed.

## Choose the readiness mode

- If the current branch has an existing PR, assess whether the exact PR head is
  **ready to merge**.
- Otherwise, assess whether the current branch and local work are **ready to
  open** as a PR.

If this is not a Git repository or no defensible base can be established, stop
and explain what is missing.

## Establish scope

Resolve the base in this order:

1. The supplied base ref, when it is not `<not supplied>`.
2. The existing PR's base branch.
3. The remote default branch, preferring `upstream` and then `origin`.
4. A single plausible trunk: `main`, `master`, `develop`, or `trunk`.
5. Ask when multiple candidates remain plausible.

Do not mistake the feature branch's tracking branch for its PR base. Refresh the
chosen remote base with a bounded fetch when network access is available. If it
cannot be refreshed, make base freshness an explicit concern.

Record:

- current branch, HEAD, and initial working-tree status;
- how the base was chosen, its freshness, and the merge base;
- commits and committed diff in the PR range;
- staged, unstaged, and untracked local changes;
- meaningful ahead/behind state.

Read applicable repository instructions. Infer the intended outcome from the
request, branch, commits, diff, and linked PR.

Keep these scopes separate throughout the audit:

1. **PR content** — committed changes in the merge-base range.
2. **Local-only work** — staged, unstaged, and untracked changes not present in
   the PR content.
3. **Pre-existing defects** — failures not introduced by this change.

Local-only work is blocking only when the submitted change needs it to function
or it demonstrates that the intended change is incomplete. Never attribute a
pre-existing failure to this change without evidence.

## Inspect

Read the complete relevant diff and enough surrounding code to understand the
changed behavior. Verify search results in context. Focus on defects that affect
submission or release:

- conflict residue, credentials, private material, or accidental artifacts;
- broken, unsafe, debug, or unfinished behavior that can ship;
- omitted migrations, generated files, configuration, docs, or changelog work;
- dependency or lockfile changes unsupported by the intended outcome;
- meaningful regression paths without focused coverage;
- unrelated changes that make the PR unsafe or difficult to review;
- local-only work required by the committed change.

Use a read-only subagent only when the diff is large or specialized review would
materially improve confidence. Verify important delegated findings yourself.
Do not report speculative concerns as findings.

## Validate

Discover relevant checks from repository instructions, contribution docs, task
runners, package scripts, tool configuration, and CI workflows. Run the
smallest useful non-fixing checks already supported by the repository, expanding
to broader checks only when justified. Do not install missing tools or start
credentialed or external services.

For each check, record the command and outcome. A failure is attributable to the
change only when the diff or a focused reproduction supports that conclusion.
A relevant check that cannot run is a concern unless the exact reviewed commit
has trustworthy equivalent CI evidence.

In merge-readiness mode, inspect available forge state for the exact head commit:

- mergeability and conflicts;
- draft status;
- required CI and whether results apply to the exact HEAD;
- required approvals or change requests;
- unresolved review conversations when the forge exposes them reliably.

Do not treat stale CI, an irrelevant matrix job, or an unavailable optional
integration as a passing check. Recheck working-tree status after validation and
report anything the audit created.

## Verdict

Use evidence to choose one heading:

- **This is not ready to open.** / **This is not ready to merge.** — a concrete
  defect makes submission unsafe, leaves intended behavior incomplete, or causes
  required validation to fail because of this change.
- **This is ready to open, with concerns.** / **This is ready to merge, with
  concerns.** — no blocker exists, but a real non-blocking concern remains.
- **This is ready to open.** / **This is ready to merge.** — the scope is
  focused, no release-affecting defect was found, and relevant validation passed.

Coverage limitations never count as successful checks. Answer immediately with
the verdict and one or two sentences explaining the decisive evidence. Include
only useful, non-empty sections:

- **Fix first** — blocking findings with `path:line` evidence.
- **Concerns** — non-blocking findings.
- **Checks** — commands and outcomes.
- **Not verified** — skipped checks, stale base/CI, and coverage limits.
- **Scope** — mode, base, reviewed commits/files, PR content versus local-only
  work, and final working-tree state.

Order findings by impact. Do not print internal severity labels, raw diffs,
long logs, empty sections, or generic praise. Keep a clean verdict short.
