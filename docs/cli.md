# CLI

> **Transitional CLI reference.** Public token, MCP, handoff, and generic-agent
> commands described here are being removed. The only v1 CLI-adjacent concern is
> private operator configuration for an isolated Codex or Claude Code review
> runner; see [agent-runtimes.md](agent-runtimes.md).

`diffuse` is the command-line interface to a self-hosted installation. Its
subcommands onboard and manage indexed repositories (`repository`) and
cross-repository context clusters (`cluster`), inspect and moderate
feedback-derived rules (`learning`), create and revoke scoped service tokens
(`token`), inspect and migrate the PostgreSQL schema (`database`), inspect or
verify the configured review model (`model`), sign in to and inspect agent CLIs
Diffuse can host (`agent`), score a labeled review-quality evaluation set
(`evaluate`), and review the current local branch (`review`).

The production image's `diffuse` entrypoint is a superset of the packaged CLI:
alongside the subcommands below it takes `serve`, `worker`, and `healthcheck`,
which is how Compose starts the API and worker. A `pip install` of this package
maps `diffuse` to the CLI only, so `diffuse serve` outside the image exits with
`invalid choice: 'serve'`.

## Installing

Install the CLI into a local Python environment with `uv pip install -e .` (or
an equivalent Python installer) to manage the self-hosted server and review a
local branch using the same index, policy, and learned rules:

```bash
diffuse repository list
diffuse cluster list
diffuse learning list 1
diffuse review
diffuse review -b origin/main --diff
diffuse review --json
diffuse review --agent
diffuse review --resume
diffuse model
diffuse model --live
diffuse agent status
diffuse evaluate evals/baseline.example.json
```

`diffuse token` is legacy public-surface plumbing and is not part of v1. New
deployments must not issue service tokens.

## Review runtimes

`REVIEW_RUNTIME` selects what produces a review. See
[agent-runtimes.md](agent-runtimes.md) for the full split.

- **`litellm` (default, only selectable value today)** — transitional one-shot
  structured passes against `REVIEW_MODEL`. Used by the self-hosted API/worker
  and by `diffuse review` until Gate C moves review onto the isolated
  agent-runner. Pass/chunk/verifier variables in
  [`.env.example`](../.env.example) apply to this runtime only.
- **`claude` / `codex` (destination)** — CLI-native sessions on the isolated
  agent-runner under a short-lived session capability. The worker never
  executes those CLIs. **Not selectable yet** (Gate B/C).

`diffuse agent` manages host plumbing for those CLIs:

```bash
diffuse agent status                    # installed, current, and signed in?
diffuse agent login claude              # Claude's own auth into ~/.diffuse/agent/claude
diffuse agent login codex               # Codex's own auth into ~/.diffuse/agent/codex
diffuse agent login codex --device-auth # headless server: no browser needed
diffuse agent write-policy claude       # restore the sandbox policy without signing in
diffuse agent write-policy codex
```

`diffuse agent login claude` runs `claude auth login` with `CLAUDE_CONFIG_DIR`
pointed at Diffuse's directory. Pick whatever Claude offers — Claude.ai
subscription, Anthropic API key, or a third-party / gateway option.

`diffuse agent login codex` runs `codex login` with `CODEX_HOME` pointed at
Diffuse's directory (ChatGPT OAuth by default; Codex's own
`login --with-api-key` stdin path if you prefer an API key). Diffuse does not
collect API keys or reimplement either vendor flow.

Arguments after the CLI name go to the vendor command unchanged, which is how
you sign in on a machine with no browser — the usual case for a self-hosted
operator. `--device-auth` prints a URL and a one-time code you complete from
anywhere; the default Codex flow instead waits on a browser reaching the host's
own localhost. Codex's `-c` / `--config` and `-p` / `--profile` are refused,
because they rewrite or select the `config.toml` Diffuse just persisted; see
`docs/agent-runtimes.md`.

`diffuse agent status` reports the version floor as well as the sign-in. Claude's
sandbox settings are version-gated and are *silently ignored* by older builds,
so Diffuse refuses to run below the floor and names the settings that would have
been dropped rather than reviewing behind a weaker boundary than the policy on
disk describes. Codex has no floor yet, so any parseable version passes; setting
one means first measuring which settings older builds silently drop. On native
Windows,
where these CLIs do not provide the OS sandbox Diffuse relies on, the runtime is
refused outright.

## Reviewing a local branch

The checkout must correspond to an enabled, indexed Diffuse repository; the CLI
identifies it from `origin`. By default it reviews tracked committed, staged,
and unstaged changes against the merge base, and untracked files are reported
but excluded unless `--include-untracked` is supplied. `--json` emits the
versioned `diffuse-cli-review-v1` document and `--agent` emits terminal-safe
plain text; progress goes to **stderr** and only when stderr is a terminal, so
both stdout documents are byte-identical interactively and in CI. An
interrupted run stores no source or model output, and `--resume` retries only
while every input identity still matches. `diffuse evaluate` scores a suite of
labeled findings against a recorded run — see
[review quality](../README.md#review-quality-is-not-measured-yet). Run `--help`
on any subcommand for the full flag reference and worked examples.

## Exit codes

Every `diffuse` command uses the same table, so scripts can branch on failure
class instead of parsing stderr:

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | Success | The command completed; a review may still report findings |
| `1` | Findings reported | `diffuse review --fail-on-findings` found at least one finding |
| `2` | Usage error | Unknown flag, missing argument, or an invalid argument value such as a malformed `--base` |
| `3` | Configuration or environment error | `DATABASE_URL` unreachable, missing or rejected `OPENAI_API_KEY`, repository not onboarded, no compatible index |
| `4` | Internal error | An unexpected Diffuse defect; please report it |

Failures print a single `diffuse: error: <message>` block on stderr, with
multi-line tool output indented beneath it and no traceback or irrelevant usage
dump. Messages name the environment variable to fix and never print a
credential: connection URLs are shown with the password replaced by `***`. Set
`DIFFUSE_CLI_TRACEBACK=1` to re-raise the original exception when filing a bug.

A CI gate therefore looks like:

```bash
diffuse review --json --fail-on-findings > review.json
case $? in
  0) echo "clean" ;;
  1) echo "findings reported"; exit 1 ;;
  *) echo "diffuse could not run"; exit 1 ;;
esac
```

The codes are stable and CI depends on them; they are defined once in
[`service/cli/review.py`](../service/cli/review.py) and repeated in every
`--help` epilog.
