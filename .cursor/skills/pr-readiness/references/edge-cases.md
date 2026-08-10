# Degraded conditions

Situations that would otherwise make the audit fail, stall, or quietly guess. Handle the condition
explicitly, say in the report what it cost, and continue with everything still checkable.

Two rules hold throughout: **never change the environment to make a check runnable** — no installs,
no starting services, no seeding databases, no deepening a clone without approval — and **never
tidy the working tree**, because stashing or committing to "clean up" destroys the very state under
audit.

## Working tree and history

| Condition | Handling |
| --- | --- |
| **Dirty working tree** | Audit committed and uncommitted work separately. Uncommitted changes the change needs to function are a blocker; unrelated local noise is a warning. Never stash or commit. |
| **Staged but uncommitted work** | Include it in the diff under audit and say it is not yet committed — it would be absent from the pushed branch. |
| **Detached HEAD** | Report it and stop; there is no branch to open a request from. Offer to identify the commit's branch. |
| **Unborn branch (no commits)** | Report that there is nothing to audit. |
| **Branch equals base, or no commits ahead** | Report that the branch has no change to propose. |
| **In-progress merge, rebase, or cherry-pick** | Treat as a blocker: the tree is mid-operation and the diff is not meaningful. |
| **Merge commits on a rebase-only repository** | Warning, citing the repository's stated convention. |

## Remotes, bases, and forges

| Condition | Handling |
| --- | --- |
| **No upstream branch** | Audit against the resolved base; note the branch is unpushed. Pushing is the user's call. |
| **No remote at all** | Audit against a local base and say base staleness is unverifiable. |
| **Shallow clone** | The merge base may be unreachable. Say so; deepen only with approval (`git fetch --deepen`). |
| **No network** | Use local refs; mark base freshness and request metadata unverified. |
| **No forge CLI, or unauthenticated** | Skip request metadata silently; it is not an error. Fall back to the remote default branch. |
| **Fork** | The base usually lives on `upstream`, not `origin`. Confirm which remote the request targets. |
| **Stacked branch** | The base is the parent feature branch, not trunk. Audit only this branch's own commits. |
| **Several plausible trunks** | Ask rather than guessing; gitflow and release-train repositories punish a wrong base. |
| **Protected-branch rules unreadable** | Common permission error. Record required checks as unverified. |

## Tooling and ecosystem

| Condition | Handling |
| --- | --- |
| **Missing tool or dependency** | Never install. Record the check as skipped with the missing tool named. |
| **Project pins a toolchain version you do not have** | Do not substitute a globally installed version. Skip, and say the pinned version was unavailable. |
| **Unrecognized ecosystem** | Fall back to CI definitions, then repository instructions. If nothing is discoverable, audit by reading alone, say exactly that, and ask the user for the project's check commands. |
| **No CI configuration** | Say CI parity is not applicable and judge on local evidence. |
| **Monorepo** | Scope to affected packages plus anything importing them. Say what was left unaudited. |
| **Non-git VCS** | Detect by marker directory — Jujutsu `.jj`, Mercurial `.hg`, Sapling `.sl`, Subversion `.svn`. A Jujutsu repo usually contains a `.git` directory too, so check for `.jj` first or it reads as plain git. Use the detected VCS's equivalents for status, log, diff, and merge base, and name it in the report. |

## Content the audit cannot see through

| Condition | Handling |
| --- | --- |
| **Submodule pointer changes** | Flag the pointer move; the submodule's own contents are out of scope unless checked out. |
| **Git LFS pointers** | Content is unverifiable without fetching. Flag pointer-only changes. |
| **Large binaries or generated bundles** | Judge by path, size, and whether they belong in the repository — not by reading them. |
| **Encrypted or sealed secrets** (`sops`, `sealed-secrets`, `git-crypt`) | Do not attempt decryption. Confirm the file is the expected encrypted form, and flag any plaintext that should have been sealed. |
| **Vendored dependencies** | Distinguish a deliberate vendoring convention from an accidental commit by checking whether the directory is already tracked. |

## Scale

| Condition | Handling |
| --- | --- |
| **Very large diff** | Beyond roughly 100 files or 3,000 changed lines, read hand-written source in full and sample generated output, lockfiles, and snapshots. Disclose the sampling. |
| **Very long validation** | Bound each command with an explicit timeout. Prefer a scoped run over a whole-repo run, and say the run was scoped. |
| **Expensive delegated review** | Ask before launching a deep or multi-agent review on a large diff; it spends the user's budget. |

When several conditions stack — a shallow fork clone with no network and an unknown stack — say
plainly that the audit is substantially unverified and give the verdict only on what was checkable.
