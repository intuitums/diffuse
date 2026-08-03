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
- `diffuse agent status` / `write-policy`
- Diffuse-owned config under `~/.diffuse/agent` (`DIFFUSE_AGENT_HOME`)
- Child environment allowlist, sandbox policy, version floor (2.1.219+)
- Native Windows refused (no OS sandbox)

What has **not** landed: an adapter that spawns the CLI for a review, or lets
`diffuse review` run without `REVIEW_MODEL`.

**Preflight for Phase 3 (agent adapter):**

| Item | Status |
| --- | --- |
| `ReviewRequest` seam | Done — runtimes take a request object |
| Internal `search_code` tool provider + recorders | Done — agent path can log calls |
| D1 baseline capture | **Owner** — needs a real `REVIEW_MODEL` credential (~$0.75) |
| R1 licensing | **Owner** — redistributing a tool that drives subscriber CLIs |
| U3 `diffuse agent login claude` UX | **Owner machine** — interactive `claude auth login`; Claude Code must be installed |

Near-term delivery order lives in the working plan at
`.context/agent-cli-runtime-plan.md` (gitignored). Summary:

1. ~~`ReviewRuntime` seam~~ done
2. ~~Claude Code host plumbing~~ done
3. Claude Code adapter — next; blocked on owner items D1 (baseline fund) and R1 (licensing)
4. Retire pass scaffolding only after eval parity
5. Codex host + adapter
6. Measure per runtime

## Pass scaffolding

`REVIEW_PASSES`, diff chunking, the pre-fused context blob, and the verifier
*pass* belong to the one-shot API runtime. They are not the review contract.
An agent runtime investigates once with tools (`search_code` / `ask_codebase`
over the index). Deleting that scaffolding waits on measured parity — see
[engineering-plan.md](engineering-plan.md).
