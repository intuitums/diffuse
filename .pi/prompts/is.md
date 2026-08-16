---
description: Analyze GitHub issues (bugs or feature requests)
argument-hint: "<issue-URL> [issue-URL...]"
---

# Analyze GitHub issues

Analyze these GitHub issues:

`${ARGUMENTS:-<not supplied>}`

If no issue URL was supplied, ask for one.

For each issue:

## Triage

1. Read the full issue, including every comment and all linked issues and pull
   requests relevant to understanding it.
2. Before beginning code analysis, use the GitHub CLI to:
   - add the `inprogress` label;
   - assign the issue to the currently authenticated GitHub user;
   - add exactly one type label when the issue is classifiable (see below).

   Use the repository's existing labels only. Preserve every label already on
   the issue. If the issue cannot yet be classified, add only `inprogress`. If
   any label or assignment action fails, report the failure and continue with
   the analysis.

### Labels

Apply these when triaging:

| Label | When to use |
| --- | --- |
| `inprogress` | Always, when analysis begins. |
| `bug` | Something is broken or incorrect. |
| `feature` | New capability or enhancement. |
| `documentation` | Docs-only change; no code behavior change. |
| `question` | Needs clarification before it can be analyzed as bug or feature. |

When analysis shows the issue should not proceed, you may also add:

| Label | When to use |
| --- | --- |
| `duplicate` | Same as an existing issue or pull request. |
| `invalid` | Not a valid bug or request. |
| `wontfix` | Valid, but intentionally out of scope. |

Do not add `good first issue`, `help wanted`, or other curation labels unless
the user explicitly asks.

## Analyze

3. Treat analysis and proposed solutions in the issue as unverified.
   Independently reproduce or verify the described behavior when practical,
   trace the relevant code and execution path, and derive your own conclusions
   from evidence.

4. For a **bug**:
   - Do not assume the issue's root-cause analysis is correct.
   - Read every relevant code file in full; do not reason from truncated excerpts.
   - Trace the execution path and identify the actual root cause.
   - Propose a focused fix.

5. For a **feature request**:
   - Do not assume implementation proposals in the issue are correct.
   - Read every relevant code file in full; do not reason from truncated excerpts.
   - Propose the simplest implementation that fully addresses the request.
   - List the affected files and the changes each one needs.

6. For **documentation** or **question** issues, analyze what is missing or
   unclear and propose the minimum follow-up needed to unblock work.

Do not implement anything unless explicitly asked. Analyze and propose only.
