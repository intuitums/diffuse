# Model execution

Diffuse owns the entire review workflow. Codex and Claude are optional
structured-generation backends; they are not alternate GitHub Apps and they do
not run the Diffuse product.

## What a CLI receives for a pull-request review

For each bounded review stage, Diffuse constructs:

- a trusted system prompt naming the review pass and invariant safety rules;
- one policy-filtered diff chunk;
- bounded retrieved context selected from Diffuse's immutable index;
- the applicable Diffuse repository policy;
- a JSON Schema for exactly one response type;
- an output limit and absolute deadline.

Repository-controlled material stays inside labeled untrusted-data blocks.
The request fingerprint covers the prompts, schema, model target, and limit.

The CLI does not receive:

- a repository checkout or arbitrary filesystem path;
- GitHub App, relay, database, or Diffuse API credentials;
- provider API keys from the worker environment;
- an MCP server, plugin, hook, tool list, or caller-selected CLI argument;
- authority to publish or update the pull request.

The response returns to the worker, is schema-validated again, checkpointed,
deduplicated, grounded against changed lines, independently verified, persisted,
and only then published by the Diffuse App.

```text
GitHub webhook
  -> Diffuse App and hosted relay
  -> self-hosted worker
  -> policy + diff + indexed context
  -> StructuredGenerator
       -> LiteLLM provider API
       -> local runner -> Codex/Claude CLI
  -> Pydantic validation + durable stage
  -> Diffuse finding validation and persistence
  -> Diffuse App publication
```

## Configuration

Environment configuration remains compatible:

```dotenv
REVIEW_EXECUTOR=litellm
REVIEW_MODEL=anthropic/claude-sonnet-5
REVIEW_VERIFIER_MODEL=
DIFFUSE_MODEL_RUNNER_SOCKET=/run/diffuse/model-runner.sock
```

Supported executor names are `litellm`, `codex-cli`, and `claude-cli`. CLI
execution requires native schema output and rejects `REVIEW_API_BASE`.

Non-secret routing can instead live in an optional TOML file:

```toml
[models]
executor = "codex-cli"
review_model = "default"
verifier_model = "default"
structured_output_mode = "schema"

[model_runner]
socket = "/run/diffuse/model-runner.sock"
```

Set its absolute path with `DIFFUSE_CONFIG_FILE`. Environment variables take
precedence. Do not put provider keys or CLI credentials in this file.

## Host runner

Install the Codex and/or Claude CLI plus Diffuse from the matching tagged source
into a Python 3.12 virtual environment for a dedicated, unprivileged host
account. (The container image does not install a host executable.) Log in
interactively as that same account:

```bash
python3.12 -m venv /home/diffuse-runner/.local/share/diffuse/venv
/home/diffuse-runner/.local/share/diffuse/venv/bin/pip \
  install /path/to/matching/Diffuse-source

codex login
claude auth login
```

Diffuse does not receive the login result. At runner startup it asks
`codex login status` and `claude auth status` only for a success exit code,
after first verifying the executable and required flags. Account identity and
raw CLI output are never returned over the socket.

For a native worker running as the same account:

```bash
diffuse-model-runner \
  --socket /run/diffuse/model-runner.sock \
  --codex-executable /absolute/path/to/codex \
  --claude-executable /absolute/path/to/claude
```

The socket is `0600` by default. `diffuse model-runner` is an equivalent
entry point. `--test-fake-backend` exists only for deterministic tests and
must never be used in production.

### Containerized worker

A container user cannot connect to a same-user-only host socket. Create one
shared host group, run the bridge with that group, and add the container worker
to the same numeric GID. A systemd service can create the volatile `/run`
directory on every boot:

```ini
[Unit]
Description=Diffuse local model runner
After=network-online.target

[Service]
User=diffuse-runner
Group=diffuse-model-runner
Environment=HOME=/home/diffuse-runner
Environment=USER=diffuse-runner
RuntimeDirectory=diffuse
RuntimeDirectoryMode=0750
ExecStart=/home/diffuse-runner/.local/share/diffuse/venv/bin/diffuse-model-runner \
  --socket /run/diffuse/model-runner.sock \
  --socket-group diffuse-model-runner \
  --codex-executable /home/diffuse-runner/.local/bin/codex \
  --claude-executable /home/diffuse-runner/.local/bin/claude
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Create the `diffuse-runner` user and `diffuse-model-runner` group before
installing the unit, then start it before Compose so `/run/diffuse` already
exists:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now diffuse-model-runner
sudo systemctl status diffuse-model-runner
```

Set `DIFFUSE_MODEL_RUNNER_GID` to the numeric GID of
`diffuse-model-runner`, then opt into the socket mount:

```bash
docker compose \
  -f deploy/compose.yaml \
  -f deploy/model-runner.compose.yaml \
  up -d
```

Those paths are for a source checkout. In the release bundle, use
`compose.yaml` and `model-runner.compose.yaml` in the current directory.

The override refuses to create a missing host directory and mounts it read-only
into the worker. The worker startup check requires the selected executor to be
present in runner health, so a missing executable, unsupported version, failed
login, or inaccessible socket stops the worker before it leases jobs.

Verify the complete path—including one small schema-constrained inference—with:

```bash
docker compose \
  -f deploy/compose.yaml \
  -f deploy/model-runner.compose.yaml \
  run --rm worker model --live
```

### Adapter restrictions

Codex runs one ephemeral app-server thread with a temporary `CODEX_HOME` that
links only the CLI-owned authentication record; user config, rules, skills,
plugins, and history are not loaded. The runner capability-gates and disables
shell/unified-exec, multi-agent, app, plugin, browser, computer-use, image,
hook, skill, and related tool features, disables web search, supplies an empty
working directory and read-only/no-network policy, and uses restricted readable
roots when that installed app-server protocol supports them. The response
schema is a native `turn/start` constraint. It uses Codex's `on-request`
approval policy with `auto_review` as the approval reviewer—the native
**Approve for me** behavior—so an unattended request never waits for a human
permission response. Automatic review does not expand the read-only sandbox.

Claude runs print mode with safe mode, no setting sources, an explicitly empty
MCP configuration and tool set, disabled skills and Chrome integration, and no
session persistence. Every invocation uses Claude's native `auto` permission
mode, which routes potential approvals through its safety classifier rather
than an invisible interactive prompt. User content is sent over stdin; trusted
instructions use Codex's developer-instruction field or a runner-owned Claude
system-prompt file. Request content is never placed on argv.

The runner strips provider keys and every Diffuse, database, relay, and GitHub
credential from the child environment. It preserves only the OS identity,
locale, proxy/certificate routing, and CLI home locations required for the
client's own stored login. Each request has a bounded deadline and output size;
canceling a superseded review terminates the CLI process group.

CLI subscription and provider limits still apply. A quota failure is normalized
as `model_rate_limited`; authentication and unsupported-version failures are
non-retryable, while raw stderr remains local and is not published to GitHub.
