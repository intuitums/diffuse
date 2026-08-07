# Review runtimes

Diffuse owns the review **contract** — the diff, policy, `ReviewReport` schema,
finding lineage, and publication. A *review runtime* turns a diff plus context
into that report. The seam is the whole report, not a single model call.

## Authority

The active architecture is the Linear
[CLI-native agent operation plan](https://linear.app/intuitum/document/cli-native-agent-operation-plan-6ccec382faef).
This document tracks what has landed in the repository against that plan.

## Target architecture

```
GitHub webhook → Diffuse queue/index/database → session capability
→ isolated agent-runner → Claude Code or Codex CLI
→ schema-validated read-only findings → Diffuse policy/publish path → GitHub App
```

| Role | Owns |
| --- | --- |
| Diffuse control plane (`app` + `worker`) | GitHub App identity and publication, webhooks, queueing, mirrors, indexing, database, policy, audit, evaluation, capability minting |
| Isolated agent-runner | Pinned Claude Code / Codex CLIs, agent credential/config storage, immutable review workspace mounts, allowlisted model-auth egress |
| Agent CLI session | Read-only investigation via scoped Diffuse tools; structured answer only |

Non-negotiable boundaries:

* The worker never executes a CLI and never mounts agent credentials in the
  target architecture.
* Agents do not receive database credentials, GitHub App credentials, arbitrary
  HTTP access, or direct GitHub write capability.
* Agents are read-only in this delivery. Diffuse alone validates and publishes.
* Running repository code is **not** part of this delivery.
* A deployment must explicitly choose `claude` or `codex`. There is no default
  runtime once LiteLLM is removed (Gate E).

## Transitional state (today)

| Path | Runtime today | Destination |
| --- | --- | --- |
| Self-hosted server | `litellm` (one-shot structured API calls) | Explicit `claude` or `codex` over the isolated agent-runner |
| Local CLI (`diffuse review`) | `litellm` | Same CLI-native session contract once adapters select it |

`REVIEW_RUNTIME=litellm` remains the only selectable implementation until Gate C
lands a runner-backed adapter. It is transitional scaffolding, not the product
direction. Do not add new LiteLLM call sites or design features that assume the
worker spawns a vendor CLI.

### Container review boundary

The supported Ubuntu/Docker posture cannot run Claude Code's Bubblewrap
sandbox (DEV-327). For the agent-runner, the container is the boundary: non-root,
no control-plane credentials, no database reachability, intended egress only.
`CONTAINER_COMPARTMENT_PROFILE` and the review-compartment preflight encode that
requirement. Evidence is in [SECURITY.md](../SECURITY.md).

## What has landed

| Piece | State |
| --- | --- |
| `ReviewRuntime` / `ReviewRequest` seam | Done |
| `report_assembly` extracted from the LiteLLM engine | Done (DEV-328) |
| Claude + Codex host plumbing (`diffuse agent login\|status\|write-policy`) | Done |
| Durable agent credential home (`DIFFUSE_AGENT_HOME`) | Done (DEV-333) |
| Review compartment preflight + CONNECT egress proxy | Done (DEV-331) |
| `service.agents` session primitive + Claude argv/MCP bridge + record/replay | Done (DEV-329); no production callers yet |
| Session capability + structured result contracts | Done (DEV-338 / Gate A) |
| Dedicated agent-runner image with pinned CLIs | In progress (DEV-330 / Gate B) |
| Capability tool HTTP endpoint + runner service | Not started (Gate B) |
| Review / Q&A / conversation / learning over CLI sessions | Not started (Gates C–D) |
| Delete LiteLLM + provider config surface | Not started (Gate E) |

## Session capability contract

`service.agents.capability` defines the short-lived bearer a runner presents to
scoped read-only tools. Claims are pinned to repository, snapshot, optional PR,
head SHA, runtime, profile, operation allowlist, request budget, and expiry.
Audit logs use `SessionCapability.as_audit_dict()` — never the bearer token.

Allowed operations today (read-only):

* `search_code`, `read_diff`, `read_file`, `read_symbol`
* `read_pr_metadata`, `read_review_threads`, `read_prior_findings`, `read_policy`

`service.agents.result.AgentSessionResult` is the runner→control-plane answer
shape: success with `value`, or failure with a stable `error_code`, named
`stage`, and `audit_ref`. Spend is turns + wall time; do not invent provider
token counts the CLI does not expose.

## Agent CLI host plumbing

Still useful for local development and for the eventual runner image:

* `diffuse agent login claude|codex` — vendor auth into `~/.diffuse/agent/...`
* `diffuse agent status` / `write-policy` / `logout`
* Child environment allowlist; Claude sandbox policy + version floor (2.1.219+);
  Codex TOML policy (`sandbox_mode = "read-only"`)

Production login belongs on the agent-runner once that service owns the
credential volume (Gate B). Until then, compose still mounts `agent_data` on
`worker` so operators can sign in — that is transitional, not the target.

### Signing in on a server with no browser

```bash
# Transitional (worker still mounts agent_data):
docker compose run --rm worker agent login codex --device-auth
docker compose run --rm worker agent login claude --console

# Target (Gate B+): same commands against the agent-runner service.
```

Arguments after the CLI name are forwarded to the vendor command unchanged.
Codex `-c` / `--config` / `-p` / `--profile` are refused because they rewrite
Diffuse-owned policy keys.

## Open adapter problems

* **Credential reach under `agent_environment` on macOS** — Keychain needs
  host identity the review child must not inherit (DEV-316).
* **Read policy for a worktree inside `$HOME`** — `denyRead` / `allowRead`
  overlap is unmeasured for paths under the real home.
* **Release-supported auth modes** — verify each offered subscription / API-key
  / third-party mode before exposing it (DEV-326).

## Delivery order

1. ~~Freeze contract / replace unsafe assumptions~~ Gate A (DEV-338)
2. Isolated runner image + capability tools — Gate B (DEV-330+)
3. Move review execution onto CLI sessions — Gate C (DEV-332)
4. Move Q&A, conversation, rule learning — Gate D (DEV-335)
5. Delete LiteLLM, provider config, legacy tests/docs — Gate E (DEV-336)
6. Pilot + cutover — Gate F

Code execution, shell access, branch mutation, and GitHub/database writes from
agents remain deferred behind a separate sandbox design.
