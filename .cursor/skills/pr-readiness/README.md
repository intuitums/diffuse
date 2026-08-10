# PR Readiness

A Cursor skill that audits the current branch and answers one question decisively: **is this
ready to become a pull request?** It is tuned for Cursor — it delegates review depth to
Bugbot and Security Review. Sibling copies tuned for other agents live in their own skills folders.

It resolves the base branch, reviews the full diff plus uncommitted and untracked work, scans for
release blockers, discovers and runs the repository's own validation commands, checks those against
what CI will run, and returns **READY**, **READY WITH WARNINGS**, or **NOT READY** with the evidence
behind the verdict.

It is audit-only. It never commits, pushes, opens a request, or edits files without explicit
approval.

It is a release gate, not a code reviewer. It delegates correctness and vulnerability depth to
Bugbot and Security Review and folds their findings into the verdict. For a review on its own, run
`/review-bugbot` or `/review-security`
directly.

Call `/review-bugbot` or `/review-security` directly — `/review` only asks which of the two to
run. A local Bugbot run shares a patch ID with the later PR review, so pushing the same diff is
not reviewed or billed twice.

> **Executing this skill?** Read `SKILL.md` — that is the workflow. This file is orientation for
> people evaluating or adopting the skill, and contains nothing needed to run it.

It lives in `~/.cursor/skills/`. Copy it to a repository's `.cursor/skills/` to share it with a
team. No build
step and no dependencies; Cursor discovers it on the next session.

## Use

Ask in plain language:

> Are we PR ready?

Other phrasings that trigger it: *is this ready for a pull request*, *can I open a PR*, *ready to
push/submit/raise a PR*, *ready to open a merge request*, *is this ready for review*, *is this ready
to merge*, *is this branch shippable*, *PR check*. It can also be invoked explicitly as
`/pr-readiness`.

The scan script is usable on its own, outside any agent, as a pre-push sanity check:

```bash
bash ~/.cursor/skills/pr-readiness/scripts/pr-scan.sh          # resolves a base itself
bash ~/.cursor/skills/pr-readiness/scripts/pr-scan.sh develop  # or name one
```

## Requirements

- **git** — required.
- **awk, grep, sed** — required; POSIX usage only, works on GNU and BSD userlands.
- Everything else is optional and detected at runtime. A missing forge CLI, secret scanner, linter,
  or language toolchain degrades that one check to "skipped" with a stated reason. Nothing is ever
  installed on your behalf.

Other version control systems (Jujutsu, Mercurial, Sapling, Subversion, Perforce) are handled by the
skill's judgment phases using their own equivalents; the helper script is git-only.

## What it does not assume

No language, framework, forge, CI system, or project layout. Checks are derived from the repository
in front of it, in this order: repository instructions (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`,
README), then the task runner, then manifests and tool configuration, then CI definitions, then
established convention for whatever ecosystems are actually detected — and it asks rather than
guessing when a repository is undiscoverable.

It never reports a check for a stack the repository does not use, and never claims a check passed
unless it actually ran.

## Safety

- **Read-only by default.** No commit, stage, push, request creation, history rewrite, dependency
  install, or file edit without explicit approval.
- **Check modes only.** Formatters run as `--check`/`--dry-run`; the writing variants are never used.
- **No dangerous commands.** Deploy, publish, release, apply, and anything touching remote or shared
  state is refused, as is anything requiring credentials.
- **Secrets are never printed.** Findings cite `path:line` and the credential type; values are
  redacted, and nothing is rotated or edited without approval.
- **Time-boxed.** Roughly five minutes per command, fifteen for validation. Expensive or unavailable
  checks are skipped and reported, never worked around.

## Layout

```
pr-readiness/
├── SKILL.md                          # the workflow the agent follows
├── README.md                         # this file
├── references/
│   ├── blocker-checks.md             # scan classes and patterns per ecosystem
│   ├── validation-discovery.md       # stack/CI detection, safety rules, parity
│   ├── edge-cases.md                 # degraded conditions: dirty trees, forks, scale
│   └── report-format.md              # output contract and worked examples
└── scripts/
    └── pr-scan.sh                    # optional read-only scan accelerator
```

`SKILL.md` is loaded when the skill triggers; the references load only when needed.

## Customizing

- **Repository-specific rules** belong in that repository's `CLAUDE.md` or `AGENTS.md`. The skill
  reads them and treats them as outranking its own defaults.
- **More ecosystems or migration frameworks**: add a row to the tables in
  `references/validation-discovery.md` or `references/blocker-checks.md`.
- **Different verdict thresholds**: edit the blocker/warning definitions in `SKILL.md`, Phase 6.
- **Scan tuning**: patterns and the per-category cap live at the top of `scripts/pr-scan.sh`.

## Limitations

- Pattern scans are advisory. They produce candidates that need review in context, and no scan
  proves the absence of a defect.
- One local run cannot clear a CI matrix; multi-version jobs are reported as partially covered.
- Checks requiring databases, containers, browsers, credentials, or the network are skipped by
  design and listed as such.
- Failure attribution to the branch versus the base is stated as unverified when confirming it would
  be expensive.
- The scan script reads up to 5,000 lines per untracked file and caps reported hits per category;
  large diffs are sampled, and the sampling is disclosed in the report.
