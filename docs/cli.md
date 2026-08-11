# CLI

> **Transitional CLI reference.** Public token, MCP, handoff, and generic-agent
> commands described historically are removed from v1. The only v1 CLI-adjacent
> concern is private operator configuration for an isolated Codex or Claude Code
> review runner; see [agents.md](agents.md).

`diffuse` is the command-line interface to a self-hosted installation. Its
subcommands onboard and manage indexed repositories (`repository`, or its
`repo` alias), inspect and migrate the PostgreSQL schema
(`database`), sign in to and inspect Agent Host CLIs (`agent`), and configure
the GitHub integration (`github`). The `cluster` (cross-repository context)
and `learning` (feedback-derived rules) subcommands are transitional: v1 scope
removes cross-repository context and defers rule learning, so do not build on
them. Keep the CLI thin: easy GitHub connect and
(later) local reviews against a Diffuse host. Connect/readiness diagnostics
belong on the web dashboard — see [v1-scope.md](v1-scope.md) → Operator
surfaces. Local-branch review is intentionally not available until it can use
the hosted Review Access Grant contract.

The production image's `diffuse` entrypoint is a superset of the packaged CLI:
alongside the subcommands below it takes the service entrypoints Compose uses
(`serve`, `worker`, `github-delivery-poller`, `agent-host`, `egress-proxy`,
`context-service`) plus their healthcheck/preflight variants;
`service/runtime.py` is authoritative. A `pip install` of this package maps
`diffuse` to the CLI only, so `diffuse serve` outside the image exits with
`invalid choice: 'serve'`.

## Installing

Install the CLI into a local Python environment with `uv pip install -e .` (or
an equivalent Python installer) to manage a self-hosted server:

```bash
diffuse repository list
diffuse repo list  # equivalent shorthand
diffuse repo settings show acme/api
diffuse repo settings set acme/api --auto-review off
diffuse maintenance reindex acme/api
diffuse cluster list
diffuse learning list 1
diffuse agent status
diffuse github connect
diffuse github status
diffuse github disconnect
```

`diffuse github connect` opens a browser against the GitHub Integration Service,
authorizes your GitHub account, and binds an App installation (install the App
first when you can). By default it labels the instance with this machine's
hostname and merges one-time secrets into `./.env` (mode `0600`), preserving
the rest of that deployment configuration. Copy `.env.example` first when
creating a new deployment. Optional overrides: `--name`, `--write-env PATH`,
`--print-secrets`, `--no-browser`, or `--code` for the advanced setup-page
fallback. `diffuse github status` checks the Integration Service binding
(ready/not-ready). `diffuse github disconnect` revokes the instance credential.
`diffuse token` has been removed. Service-token minting is not part of v1.

## Repository controls

Repository commands use the GitHub repository name (`owner/repo`). `diffuse
repo disable acme/api` stops indexing and review for that repository. For an
enabled repository, `--auto-review off` prevents GitHub pull-request events
from starting a review while preserving authorized manual `@diffuse review`
requests. Pass `--base-url URL` only if the same `owner/repo` is connected on
more than one GitHub host.

`diffuse repo add` records GitHub's immutable repository identity during
onboarding. GitHub webhook processing keeps the mutable repository name and
clone URL current automatically after a rename or ownership transfer.

## Maintenance

Normal repository indexing and rename handling happen in the backend. The
explicit maintenance command is for recovery and index-format upgrades:

```bash
diffuse maintenance reindex acme/api
diffuse maintenance reindex --all
```

`--all` queues a fresh index for every enabled repository and is safe to rerun.
It is the required operator step after an upgrade that changes the index format.

## Review runtimes

`REVIEW_AGENT` selects what produces a review. See
[agents.md](agents.md) for the full split.

- **`claude` / `codex`** — `REVIEW_AGENT=claude` or
  `codex` sends a self-hosted worker review to the isolated matching runner
  under a short-lived session capability. The worker never executes those
  CLIs. Local `diffuse review` is unavailable until it can use the same session
  contract.

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
`docs/agents.md`.

`diffuse agent status` reports the version floor as well as the sign-in. Claude's
sandbox settings are version-gated and are *silently ignored* by older builds,
so Diffuse refuses to run below the floor and names the settings that would have
been dropped rather than reviewing behind a weaker boundary than the policy on
disk describes. Codex has no floor yet, so any parseable version passes; setting
one means first measuring which settings older builds silently drop. On native
Windows,
where these CLIs do not provide the OS sandbox Diffuse relies on, the runtime is
refused outright.

## Local branch review

`diffuse review` is intentionally unavailable in the Agent-only release. Push
the branch and open or update a pull request; the configured Agent Host then
receives the short-lived Review Access Grant and produces the review. This
avoids a local process inheriting a developer's repository, credentials, or
unbounded environment.

## Exit codes

Every `diffuse` command uses the same table, so scripts can branch on failure
class instead of parsing stderr:

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | Success | The command completed; a review may still report findings |
| `1` | Command-specific failure | A command completed its work but reported a failing result |
| `2` | Usage error | Unknown flag, missing argument, or an invalid argument value such as a malformed `--base` |
| `3` | Configuration or environment error | `DATABASE_URL` unreachable, a Review Agent is not configured, repository not onboarded, or no compatible index |
| `4` | Internal error | An unexpected Diffuse defect; please report it |

Failures print a single `diffuse: error: <message>` block on stderr, with
multi-line tool output indented beneath it and no traceback or irrelevant usage
dump. Messages name the environment variable to fix and never print a
credential: connection URLs are shown with the password replaced by `***`. Set
`DIFFUSE_CLI_TRACEBACK=1` to re-raise the original exception when filing a bug.

The codes are stable and are defined in
[`service/cli/review.py`](../service/cli/review.py).
