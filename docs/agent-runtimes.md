# Agent runtimes

**Authority:** the Linear document
[CLI-native agent operation plan](https://linear.app/intuitum/document/cli-native-agent-operation-plan-6ccec382faef)
is the runtime architecture for Diffuse. This page is the in-repo mirror of that
contract. Older local-adapter / worker-spawns-CLI designs are superseded.

## Target architecture

Diffuse is the deterministic **control plane**: GitHub App identity and
publication, webhook ingestion, queueing, repository mirrors and indexing,
database persistence, policy enforcement, audit, and evaluation.

Claude Code and Codex are the **agent operation layer**. They run only inside a
credential-isolated **agent-runner**. In the target architecture:

- the worker never executes a CLI
- the worker never mounts agent credentials
- agents receive a short-lived session capability, a read-only review workspace,
  scoped Diffuse tools, and allowlisted model-auth egress
- agents do not receive database credentials, GitHub App credentials, arbitrary
  HTTP access, or direct GitHub write capability
- Diffuse alone validates structured findings and publishes as the GitHub App

```text
GitHub webhook → Diffuse queue / index / database → session capability
→ isolated agent-runner → Claude Code or Codex CLI
→ schema-validated read-only findings → Diffuse policy / publish → GitHub App
```

There is no Diffuse-hosted cloud control plane. “Hosted” in older comments means
the **self-hosted server** process, not a SaaS tier.

## What is live today vs destination

| Layer | Today (transitional) | Destination |
| --- | --- | --- |
| Review execution on the server | `litellm` transitional API path | `REVIEW_RUNTIME=codex` or `claude`, sent to an isolated matching runner |
| Worker | Runs API reviews or dispatches native sessions; never runs a CLI | Mints session capabilities, validates structured results, publishes |
| Agent credentials | Separate Compose runner volumes | Separate volume mounted only by its own runner |
| Local `diffuse review` | Same LiteLLM path until adapters move | May use the same session/capability contract against a local or remote runner |

LiteLLM remains selectable until Gate C/E so production reviews keep booting.
No *new* application path should be designed around LiteLLM or around the worker
spawning a CLI. Full removal of LiteLLM is Gate E.

## Shared contract modules

Control plane and runner share `service.agents.contract`:

| Module | Role |
| --- | --- |
| `runtime` | Explicit runtime names (`claude`, `codex`), turn budget, timeout, result size |
| `capability` | Mint / verify short-lived session capabilities (scope, operations, expiry) |
| `result` | Schema-validated session results and a stable validation-failure taxonomy |

Unit tests cover mint, scope enforcement, expiry, and the result failure
taxonomy. Gate B wires the runner image and capability tools onto this contract;
Gate C moves review execution onto it.

## Compose skeleton (Gate B reference)

Both `docker-compose.yml` and `deploy/compose.yaml` ship both opt-in runner
profiles:

- `agent-runner-claude` / `agent-runner-codex` — independently built, pinned
  CLI hosts with separate credentials, homes, and egress proxies.
- `agent-tool-gateway` — credential-free bridge exposing only approved
  `/agent/v1/tools/*` capability methods to the runners.

The `worker` and `app` services do not mount agent credentials.

```bash
# Sign in through the isolated runner (not the worker):
docker compose --profile agent-codex run --rm agent-runner-codex agent login codex --device-auth
docker compose --profile agent-claude run --rm agent-runner-claude agent login claude --console
```

## Container review boundary

The supported Ubuntu/Docker posture cannot run Claude Code's Bubblewrap
sandbox: the measured user-namespace probe failed under the default hardened
container, every tested AppArmor/seccomp/capability variation, and privileged
mode. `failIfUnavailable: true` therefore correctly prevents Claude from
silently running without its local sandbox; weakening the container until that
setting stops refusing is not an option.

For the self-hosted agent-runner, the container is the boundary. Its
dedicated compartment must prove at runtime that the process reading untrusted
content is non-root, has no control-plane credentials, cannot reach the database
or other sensitive services, and has only intended egress. The corresponding
`CONTAINER_COMPARTMENT_PROFILE` in `agent_host.sandbox_settings` disables the
unavailable CLI sandbox, and will not render at all without a
`CompartmentAssertion` from a preflight that passed — the requirement is
enforced, not annotated. The complete evidence matrix and security rationale are
in [SECURITY.md](../SECURITY.md).

## What `litellm` is (transitional)

`REVIEW_RUNTIME=litellm` names today’s one-shot API runtime. The implementation
uses the LiteLLM library to call `REVIEW_MODEL` (and optional
`REVIEW_VERIFIER_MODEL`), including self-hosted OpenAI-compatible endpoints via
`REVIEW_API_BASE`. The same library still powers repository Q&A, learning, and
conversation until Gates C–E re-home those operations.

`litellm` is an implementation label, not the product. Prefer talking about the
**transitional API / one-shot runtime** vs **CLI-native agent-runner sessions**.

## Agent CLI host plumbing

`claude` and `codex` are the CLI-native runtime names. `codex` is the default
for unknown, incomplete, ambiguous, or human-authored work. A high-confidence,
SCM-verified OpenAI identity routes to Claude; a verified Anthropic identity
routes to Codex. Commit trailers and author-controlled fields are audit evidence
only and cannot change the selected runtime.

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

**Exceptions.** Codex's `-c` / `--config` and `-p` / `--profile` are refused,
because they write or select the same `config.toml` keys Diffuse persists —
including the `cli_auth_credentials_store = "file"` that keeps the credential
inside `CODEX_HOME`. Sending it back to the OS keychain would put the
credential where a review run with `--ignore-user-config` cannot read it, and
the login would still exit 0, so the breakage would surface much later as a
review that cannot authenticate. Change the persisted policy with
`diffuse agent write-policy codex` instead. The refusal is per-CLI: those flags
mean nothing to `claude auth login`, so Claude forwards them.

Also landed:

- `diffuse agent status` / `write-policy` for both
- Diffuse-owned config under `~/.diffuse/agent` (`DIFFUSE_AGENT_HOME`)
- Child environment allowlist; Claude sandbox policy + version floor (2.1.219+);
  Codex TOML policy (`sandbox_mode = "read-only"`, shell env excludes). Codex
  has no version floor yet: setting one means first measuring which of its
  settings older builds accept and then ignore.
- Native Windows refused (no OS sandbox Diffuse can rely on)
- Review-compartment preflight, egress proxy, and credential-home image layout
- Signed, durable native sessions: the worker mints a 15-minute scoped
  capability, signs its dispatch with Ed25519, and atomically accepts only the
  matching completion (an identical retransmission is harmless; a differing
  replay is rejected).

Each Diffuse release publishes app, runner-claude, and runner-codex images.
Operators pin all three digests from the release bundle and never run a vendor
CLI updater. If authentication expires, the runner returns `agent_auth_required`;
Diffuse posts one idempotent, generic reconnect notice and never exposes a device
code, URL, account identity, token, or vendor error on the pull request. A
successful reconnect requires an explicit re-run or a new push.

Delivery order (from the plan):

1. ~~Freeze the contract (Gate A)~~ this page + `service.agents.contract`
2. Deliver the read-only agent-runner and capability tools (Gate B)
3. Move review execution to the CLIs (Gate C)
4. Move remaining intelligent operations (Gate D)
5. Destructive LiteLLM cleanup (Gate E)
6. Pilot and cutover (Gate F)

## Pass scaffolding

`REVIEW_PASSES`, diff chunking, the pre-fused context blob, and the verifier
*pass* belong to the transitional one-shot API runtime. They are not the review
contract. An agent-runner session investigates once with scoped tools over the
index. Deleting that scaffolding waits on Gate C/E measured cutover — see
[engineering-plan.md](engineering-plan.md).
