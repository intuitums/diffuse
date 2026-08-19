---
name: pr
description: File or update a GitHub pull request.
---

# File PR

Follow any request-specific instructions provided when this skill is invoked. If
none are given, create or update the pull request for the current branch.

When multiple PRs are requested, split the changes into coherent, independently
reviewable sections and apply the guidance below to each PR.

## Inspect

Inspect each current branch, HEAD, working-tree status, commits, full diff against
the target branch, remotes, and any existing pull request for the branch.

Never create a duplicate pull request. If one already exists, repair or update
that PR instead and return its URL.

## Complete the submitted change

Ensure each PR contains the complete intended change. If required work is staged,
unstaged, untracked, missing from the branch, or otherwise omitted from the PR,
repair the branch and PR so the intended change is complete.

Preserve unrelated work: do not discard, rewrite, reformat, stage, or commit
unrelated changes. If required repairs directly overlap ambiguous existing work
and cannot be separated safely, stop and ask rather than guessing.

Review the exact diff that will be submitted and make sure it matches the
intended change before creating or updating the PR.

## Publish the branch

Push each branch when necessary. Do not force-push or rewrite published history.
Create a focused commit only when required to make the intended PR complete,
using the repository's commit conventions and required trailers. Never create
an empty or unrelated commit.

## Write the pull request

Follow the repository's PR-title conventions. Inspect recent merged PRs and Git
history when useful. Prefer a concise, human-readable title that explains why
the change matters:

BAD
> ❌ perf(server): negotiate permessage-deflate on the websocket

GOOD
> ✅ perf(server): cut websocket frame size by 70%+ with gzipping

Open the description with a simple explanation of the user problem, then briefly
explain the solution and relevant behavior changes. Include meaningful validation
and important migration, compatibility, rollout, or risk notes. Do not lead with
an implementation inventory or paste a commit log:

BAD
> ❌ Removed implicit workspace carry-over from every "new thread" entry point
> (cmd+n / cmd+shift+o, sidebar v1/v2 buttons, command palette). New threads
> inherit only the project from context; branch, worktree, and env mode always
> come from the configured defaults. Deleted buildContextualThreadOptions,
> startNewThreadInProjectFromContext, and the v1 sidebar's seed-context machinery.

GOOD
> ✅ My "new worktree" default was ignored when starting new threads on existing
> worktrees. Super unintuitive. Now your preferences always apply.

Open a normal PR so review automation runs. Use a draft only when the user
explicitly requests one or the change is intentionally incomplete.

## Report

Return each PR URL and a brief summary of what was created or updated, including
any repair made before filing.
