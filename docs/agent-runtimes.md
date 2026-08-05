# Review runtimes

Diffuse owns the review **contract** — the diff, policy, `ReviewReport` schema,
finding lineage, and publication. A *review runtime* turns a diff plus context
into that report. The seam is the whole report, not a single model call.

## Two paths

| Path | Who runs it | Runtime today | Destination |
| --- | --- | --- | --- |
| Self-hosted server | API + worker on your infrastructure | `litellm` (one-shot structured passes via a model API) | Stays on an API completion runtime. Refuses agent-CLI runtimes at startup. |
| Local CLI | `diffuse review` on a developer machine | `litellm` (same one-shot path) | May select `claude` or `codex` once adapters exist — driving a locally installed, locally authenticated agent CLI behind Diffuse-owned config. |

There is no Diffuse-hosted cloud control plane. “Hosted” in older comments means
the **self-hosted server** process, not a SaaS tier.

## What `litellm` is

`REVIEW_RUNTIME=litellm` names today’s one-shot API runtime. The implementation
uses the LiteLLM library to call `REVIEW_MODEL` (and optional
`REVIEW_VERIFIER_MODEL`), including self-hosted OpenAI-compatible endpoints via
`REVIEW_API_BASE`. The same library still powers repository Q&A, learning, and
conversation even when local review later rents an agent CLI.

`litellm` is therefore an implementation label, not the product. Prefer talking
about the **API / one-shot runtime** vs **agent-CLI runtimes**. Renaming the
env value waits until a second runtime is actually selectable.

## Agent CLI runtimes (destination)

Planned values: `claude`, `codex`. Neither is in `RUNTIME_NAMES` yet, so
`REVIEW_RUNTIME` rejects them. What *has* landed:

- `diffuse agent login claude` — runs `claude auth login` into
  `~/.diffuse/agent/claude`. Auth method is Claude's own menu (Claude.ai
  subscription, Anthropic API key, or a third-party / gateway option). Diffuse
  does not collect or store API keys itself.
- `diffuse agent login codex` — runs `codex login` into
  `~/.diffuse/agent/codex` (`CODEX_HOME`), with `cli_auth_credentials_store =
  "file"` so the credential stays in that directory. ChatGPT OAuth by default;
  Codex's own API-key path if you prefer. Same rule: Diffuse does not collect
  keys.
- **Arguments after the CLI name are forwarded to the vendor command
  unchanged.** Choosing *how* to authenticate is the vendor's menu, so Diffuse
  does not mirror their flags — an allowlist here would need updating on every
  vendor release and would be silently wrong in between.

### Signing in on a server with no browser

This is the normal case for a self-hosted operator, and the default flows do
not cover it. Codex's default OAuth expects a browser that can reach a callback
on the host's own localhost; on a headless box it hangs until you build an SSH
tunnel. Use device authorization instead — it prints a URL and a one-time code
you complete from any machine:

```bash
diffuse agent login codex --device-auth
diffuse agent login claude --console   # Anthropic Console / API billing
```

Anything else the vendor accepts works the same way, including the stdin
credential paths:

```bash
printenv OPENAI_API_KEY | diffuse agent login codex --with-api-key
```

Use `--` before the vendor arguments if one ever collides with a Diffuse flag:
`diffuse agent login codex -- --device-auth`.

**One exception.** Codex's `-c key=value` is refused, because it writes the same
`config.toml` keys Diffuse persists — including the
`cli_auth_credentials_store = "file"` that keeps the credential inside
`CODEX_HOME`. Sending it back to the OS keychain would put the credential where
a review run with `--ignore-user-config` cannot read it, and the login would
still exit 0, so the breakage would surface much later as a review that cannot
authenticate. Change the persisted policy with `diffuse agent write-policy
codex` instead. The refusal is per-CLI: `-c` means nothing to `claude auth
login`, so Claude forwards it.
- `diffuse agent status` / `write-policy` for both
- Diffuse-owned config under `~/.diffuse/agent` (`DIFFUSE_AGENT_HOME`)
- Child environment allowlist; Claude sandbox policy + version floor (2.1.219+);
  Codex TOML policy (`sandbox_mode = "read-only"`, shell env excludes). Codex
  version floor pending U4 empirics.
- Native Windows refused (no OS sandbox Diffuse can rely on)

What has **not** landed: an adapter that spawns either CLI for a review, or lets
`diffuse review` run without `REVIEW_MODEL`.

**Preflight for Phase 3 (agent adapter):**

| Item | Status |
| --- | --- |
| `ReviewRequest` seam | Done — runtimes take a request object |
| Internal `search_code` tool provider + recorders | Done — agent path can log calls |
| Claude + Codex host plumbing | Done — login/status/write-policy |
| D1 baseline capture | **Owner** — needs a real `REVIEW_MODEL` credential (~$0.75) |
| R1 licensing | **Owner** — redistributing a tool that drives subscriber CLIs |
| U3 `diffuse agent login claude` UX | Done — signed in and verified on macOS; `diffuse agent status` reports `ready` |
| U4 Codex empirics | **Owner machine** — version floor + silent-ignore matrix for Codex settings |
| Credential reachable from the session | **Open (DEV-316)** — on macOS the CLI reads its credential from the login Keychain via `USER`, the real `HOME`, and `security` on `PATH`, all of which `agent_environment` removes. The read-only probes take `probe_environment`; a review cannot, so how the agent process receives its credential is undecided |
| Read policy over a worktree under `$HOME` | **Open (DEV-319)** — `denyRead` covers all of `REAL_HOME` and `allowRead` names the worktree, which for local review is normally inside it. Precedence is unmeasured and the one test covering the pair uses a `/tmp` path |

Near-term delivery order lives in the working plan at
`.context/agent-cli-runtime-plan.md` (gitignored). Summary:

1. ~~`ReviewRuntime` seam~~ done
2. ~~Claude + Codex host plumbing~~ done
3. Claude Code adapter — next; blocked on owner items D1 (baseline fund) and R1 (licensing)
4. Retire pass scaffolding only after eval parity
5. Codex adapter
6. Measure per runtime

## Pass scaffolding

`REVIEW_PASSES`, diff chunking, the pre-fused context blob, and the verifier
*pass* belong to the one-shot API runtime. They are not the review contract.
An agent runtime investigates once with tools (`search_code` / `ask_codebase`
over the index). Deleting that scaffolding waits on measured parity — see
[engineering-plan.md](engineering-plan.md).
