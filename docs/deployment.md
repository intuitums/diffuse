# Single-server deployment

This profile is for one trusted operator or a small trusted team on a Linux
server. Diffuse is still a foundation release: it does not yet provide
multi-tenant isolation, encrypted per-installation SCM credentials, operational
metrics, or automated backup retention. Do not expose it as an untrusted
multi-tenant service.

## Host preparation

Provide:

- a current Docker Engine with the Compose plugin;
- outbound HTTPS access to GitHub and the configured model provider;
- enough persistent disk for PostgreSQL and repository mirrors; and
- host-level monitoring for disk, memory, container restarts, and backup age.

The recommended hosted-relay mode requires no inbound port or public DNS name.
Add a DNS name and HTTPS reverse proxy only when exposing REST/MCP to remote
clients or when using an operator-owned GitHub App in standalone mode. Compose
binds PostgreSQL and Diffuse itself to `127.0.0.1`; keep those bindings private.

## Configure secrets

Create the deployment environment and restrict it to the operator:

```bash
# Published release bundle:
cp env.example .env
# Source checkout instead:
# cp .env.example .env
chmod 600 .env
openssl rand -hex 32
openssl rand -hex 32
```

Put the generated values in `POSTGRES_PASSWORD` and `DIFFUSE_API_TOKEN`. Hex is
recommended for `POSTGRES_PASSWORD` because Compose places it in a PostgreSQL
URL. Also set:

- `DIFFUSE_PUBLIC_URL` and `DIFFUSE_MCP_ALLOWED_HOSTS` to the external origin
  only if remote REST/MCP clients need one;
- `DIFFUSE_RELAY_URL` and `DIFFUSE_RELAY_TOKEN` for the recommended shared App,
  or the local GitHub App identity and webhook secret for standalone mode;
- the GitHub web/API origins and the narrowest possible
  `GITHUB_ALLOWED_INSTANCES`; and
- the model credentials or self-hosted model endpoint.

Every configured origin ends up carrying a token or a clone credential, so
`http://` is refused for anything other than loopback.
`DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1` lifts that for a lab instance and should
never be set in production.

Do not commit `.env`, copy it into an image, or place its values on command
lines. Back it up separately in an encrypted secret manager.

### Sourcing the environment from a secret manager

Diffuse has no built-in secret-manager integration, and choosing one is the
operator's responsibility. What it does provide is `DIFFUSE_ENV_FILE`: every
service reads `env_file: ${DIFFUSE_ENV_FILE:-.env}`, so the environment can be
rendered somewhere other than a checked-out `.env` without changing the Compose
file.

The pattern that keeps a plaintext credential off persistent disk is to render
it into a tmpfs at start time and let the unit that renders it own the
lifetime:

```ini
# /etc/systemd/system/diffuse.service
[Service]
RuntimeDirectory=diffuse
RuntimeDirectoryMode=0700
Environment=DIFFUSE_ENV_FILE=/run/diffuse/env
# Any renderer that writes KEY=VALUE lines works. Examples:
#   op inject -i /etc/diffuse/env.tpl -o /run/diffuse/env
#   aws secretsmanager get-secret-value --secret-id diffuse/prod \
#     --query SecretString --output text > /run/diffuse/env
ExecStartPre=/usr/local/bin/render-diffuse-env /run/diffuse/env
ExecStart=/usr/bin/docker compose --env-file /run/diffuse/env up -d
```

`/run` is tmpfs, so the rendered file does not survive a reboot and is never
captured by a filesystem backup. Keep the mode at `600` and the owner at the
account that runs Compose.

Doppler's Developer plan is a right-sized example for a small internal
deployment. In relay mode it stores the node credential alongside database,
API, and model credentials; the GitHub App PEM stays on the hosted gateway. In
standalone mode, put the PEM in `GITHUB_APP_PRIVATE_KEY`; Doppler's Docker
output format escapes PEM newlines, and Diffuse restores them before signing:

```bash
doppler secrets download --no-file --format docker > /run/diffuse/env
chmod 600 /run/diffuse/env
DIFFUSE_ENV_FILE=/run/diffuse/env \
  docker compose --env-file /run/diffuse/env up -d
```

The Doppler service token that authorizes this download is then the only
bootstrap credential the host must hold. Scope it to the production config;
do not put it in Diffuse's environment file.

Two properties of Diffuse constrain how rotation works:

- **Most credentials are read from the process environment.** A rotated value
  does not reach a running container; re-render and recreate the containers.
  A file-backed `GITHUB_APP_PRIVATE_KEY_FILE` is re-read when the in-memory
  installation token refreshes, but recreate app and worker when rotating it
  so the new key is exercised immediately.
- **`POSTGRES_PASSWORD` is not rotatable by editing the file.** Compose
  interpolates it into `DATABASE_URL`, and PostgreSQL stores the password in the
  data volume on first initialization. Changing it later requires an
  `ALTER ROLE` against the running database as well. Choose it before the first
  `up`.

In standalone mode, rotating `GITHUB_WEBHOOK_SECRET` has a delivery gap: the
server verifies against exactly one secret, so deliveries signed with the old
value are rejected from the moment the new one is live until GitHub's webhook
configuration is updated. Relay nodes do not hold this secret.

Whatever the source of the values, the security property that matters is that
the credential's home of record is somewhere with access control, an audit
trail, and rotation — not a file that exists only on one host. `.env` on the
server is a cache of that record, not the record itself.

### Pair with the shared Diffuse GitHub App (recommended)

Open the hosted relay's sign-in page, sign in with GitHub, and install the
Diffuse App on the intended organization or repositories. The relay's setup
page returns a single-use command containing its URL and a ten-minute pairing
code:

```bash
docker compose run --rm worker relay pair \
  --gateway https://integrations.diffuse.example \
  --code <single-use-code>
```

The command prints a `node_token` exactly once. Store `gateway_url` as
`DIFFUSE_RELAY_URL` and `node_token` as `DIFFUSE_RELAY_TOKEN`, then recreate
the app and worker. Do not put the pairing code in the long-lived environment.

The worker polls the relay over outbound HTTPS, durably ingests each routed
GitHub delivery through the existing webhook workflow, and acknowledges it
only after local acceptance. The relay buffers deliveries while this host is
offline and brokers one-hour installation tokens. It does not clone
repositories, run reviews, or receive model traffic.

In this mode leave `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
`GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_PRIVATE_KEY_FILE`, `GITHUB_TOKEN`, and
`GITHUB_WEBHOOK_SECRET` empty.

### Configure an operator-owned GitHub App (standalone alternative)

Diffuse authenticates automation as a GitHub App installation. Do not mint an
installation token by hand and do not create a PAT: installation tokens expire
after one hour, and Diffuse now creates and refreshes them itself.

Create a private GitHub App owned by the organization and configure:

- repository permissions: **Contents: read**, **Pull requests: read and
  write**, **Issues: read** (required for the `issue_comment` event), and
  **Checks: read and write** when status checks will be enabled;
- webhook events: `push`, `pull_request`, `issue_comment`, and
  `pull_request_review_comment`;
- webhook URL: `https://diffuse.example.com/webhook/github`; and
- a high-entropy webhook secret copied into `GITHUB_WEBHOOK_SECRET`.

Install the App only on the pilot repositories. Then set:

- `GITHUB_APP_ID` to the App's client ID (GitHub's recommended JWT issuer) or
  numeric App ID;
- `GITHUB_APP_INSTALLATION_ID` to the trailing number in the organization's
  installed-App settings URL; and
- one of `GITHUB_APP_PRIVATE_KEY` or `GITHUB_APP_PRIVATE_KEY_FILE`.

The default release profile is easiest with a secret manager that injects
`GITHUB_APP_PRIVATE_KEY`. If you use a file, mount it read-only into both
`app` and `worker`, set the container-visible path in
`GITHUB_APP_PRIVATE_KEY_FILE`, and use mode `600` where the platform permits.
The migration container does not need the key.

`GITHUB_TOKEN` remains only as a compatibility fallback. Leave it empty for
App authentication. A partial App configuration fails closed rather than
silently falling back to a different identity.

GitHub permits overlapping App private keys. Rotate without downtime by
generating a second key, replacing the secret-manager value, recreating app and
worker, confirming a token can be minted, and only then deleting the old key.

### Model execution and credential ownership

Provider-API execution uses LiteLLM. Choose a provider API key, ambient cloud
identity, or an operator-run compatible endpoint. The two primary provider-key
configurations are:

| Provider | Model setting | Credential |
| --- | --- | --- |
| OpenAI | `REVIEW_MODEL=openai/<model>` | `OPENAI_API_KEY` |
| Anthropic | `REVIEW_MODEL=anthropic/<model>` | `ANTHROPIC_API_KEY` |

Diffuse does not run a model-account sign-in flow and does not accept model
account access or refresh tokens. Codex CLI and Claude Code CLI execution uses
an opt-in host runner: authenticate the selected CLI as a dedicated operating
system account, start `diffuse model-runner`, and mount only its Unix-socket
directory into the worker with `model-runner.compose.yaml`. The runner
capability-gates the installed version and login before advertising an adapter;
the worker refuses to start when its selected adapter is unavailable.

The CLI process receives only Diffuse-prepared prompts and a response schema in
an empty temporary directory. It receives no checkout, GitHub/relay/database
credential, provider API key, or publication authority. See
[Model execution](model-execution.md) for the systemd and Compose setup.

Today, the way to run Diffuse without an OpenAI embedding credential is
`EMBEDDING_API_BASE` plus a non-OpenAI `REVIEW_MODEL`.

The binding constraint is that the endpoint must return 1536-dimensional
vectors, because the schema pins the column at `VECTOR(1536)`. That rules out the
most common local choices — `nomic-embed-text` is 768 and `mxbai-embed-large` is
1024, and either fails every index job with "Embedding model returned N
dimensions; expected 1536". Options that do fit:

| Endpoint | Model | How it reaches 1536 |
| --- | --- | --- |
| Self-hosted (vLLM, TEI, Ollama) | `BAAI/bge-code-v1` | native |
| Self-hosted | `jinaai/jina-code-embeddings-1.5b` | native |
| Self-hosted | `Alibaba-NLP/gte-Qwen2-1.5B-instruct` | native |
| `https://api.mistral.ai/v1` | `codestral-embed` | default output dimension |
| `https://api.jina.ai/v1` | `jina-embeddings-v4` | `dimensions: 1536` (native 2048) |

The 1536-native self-hosted models are all code-retrieval models, which is not a
coincidence: 1536 is the hidden size of the Qwen2-1.5B backbone they share. That
suits reviewing code, but do not assume it behaves like `text-embedding-3-small`
on prose.

The two hosted rows are OpenAI-compatible and take a bearer token, so they work
through `EMBEDDING_API_BASE` with `OPENAI_API_KEY` set to that provider's key —
a way to avoid an OpenAI account without running an inference server. Neither
has been exercised against a live key here, and Mistral's own documentation
contradicts itself on whether `codestral-embed` returns 1536 or 1553, so confirm
the width on the first call rather than trusting the table:

```bash
docker compose run --rm worker repository sync <repository-id>
docker compose logs worker | grep "expected 1536"
```

An index job that completes has already agreed with the constraint; one that
prints that line disagrees, and no repository will be reviewable until the model
changes.

### Embeddings with no stored credential at all

If the deployment already runs in AWS, Google Cloud, or Azure, there is a better
answer than storing any key: point `EMBEDDING_MODEL` at that cloud's provider
prefix and let the ambient identity authenticate. Diffuse resolves a credential
itself only for OpenAI model names, so for these prefixes it passes no key and
LiteLLM uses the provider's own chain — an EC2 instance profile, IRSA or EKS Pod
Identity, GKE Workload Identity, or an Azure managed identity. `EMBEDDING_API_BASE`
is not involved, and no secret reaches `.env`.

The 1536-dimension constraint still applies, and it eliminates each cloud's
default embedding model, so the combination matters:

| `EMBEDDING_MODEL` | Identity | 1536? |
| --- | --- | --- |
| `bedrock/amazon.titan-embed-text-v1` | IAM (instance profile, IRSA, Pod Identity) | yes, fixed |
| `bedrock/cohere.embed-v4` | IAM | yes, the default `output_dimension` |
| `bedrock/amazon.titan-embed-text-v2:0` | IAM | **no — 1024, fails every index job** |
| `azure/text-embedding-3-small` | Entra ID managed identity | yes, the default |
| `vertex_ai/text-embedding-005` | ADC / Workload Identity | **no — caps at 768** |

Bedrock additionally needs `boto3`, which is not a Diffuse runtime dependency.

Two caveats worth knowing before choosing this path. Azure is the only one of
the three whose vendor documentation covers keyless auth on the embeddings
endpoint specifically, and the only one that can prove keys are off by setting
`disableLocalAuth` on the resource. And `vertex_ai/gemini-embedding-001` can
reach 1536 only through an `output_dimensionality` parameter that Diffuse does
not send, so it is not usable here today even though the model supports it.

### Do not configure browser sign-in on a review node

Leave `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET_FILE`, and
`GITHUB_APP_SLUG` unset, and do not create a secrets directory for them.

The hosted relay owns those browser and App-installation callbacks. The review
node's API, MCP, and REST surfaces continue to accept `DIFFUSE_API_TOKEN` or a
repository-scoped service token; a relay credential does not authenticate
those surfaces.

Authenticate with `DIFFUSE_API_TOKEN` for bootstrap and recovery, and with
`docker compose run --rm worker token add` service tokens for routine clients.

There is no `diffuse` binary to install: every command ships inside the release
image. Anywhere this document or the bundle's `README.md` shows
`diffuse <command>`, the Compose form is
`docker compose run --rm worker <command>`.

Validate interpolation before starting anything:

```bash
docker compose config --quiet
```

Compose refuses to start without `POSTGRES_PASSWORD`, `DIFFUSE_API_TOKEN`,
`DIFFUSE_PUBLIC_URL`, and `DIFFUSE_IMAGE`. Release bundles pre-pin the last of
these to the signed release digest.

## Start and expose the service

Pull and start a release bundle's digest-pinned, migration-gated
stack:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
docker compose ps
curl --fail http://127.0.0.1:8000/ready
```

(Building from a source checkout instead uses `docker compose up -d --build`
against the repository's own `docker-compose.yml` and `.env.example`. Neither
file is part of this bundle.)

The `migrate` container must finish successfully before `app` and `worker`
start. Both the API and worker share the repository-mirror volume. The API
container has a readiness health check, and container logs rotate at bounded
sizes.

No reverse proxy is needed for relay delivery. If remote API or MCP clients
must reach the node—or if this is a standalone GitHub App deployment—terminate
TLS in a reverse proxy on the same host. For example, a minimal Caddy site is:

```caddyfile
diffuse.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Only in standalone mode, point the GitHub webhook at
`https://diffuse.example.com/webhook/github`. Enable these four event types:

- `push`
- `pull_request`
- `issue_comment`
- `pull_request_review_comment`

Verify a signed `ping` or harmless test delivery before onboarding production
repositories.

## Onboard a repository

Nothing is reviewed until the repository is registered. Diffuse does not
discover repositories from the installation:

```bash
docker compose run --rm worker repository add \
  --provider github \
  --base-url https://github.com \
  --repo owner/name \
  --default-branch main

docker compose run --rm worker repository list
```

`repository list` reports `mirror_state` and `last_error_code` per repository.
The initial index must finish before the first review can run — a repository
with no index cannot be reviewed.

### Review triggers are conservative by default

Two defaults surprise most first deployments, because they make a working
installation look broken:

- **`triggers.review_updates` is `false`.** A pull request is reviewed when it
  opens, and *not* when further commits are pushed to it. Push a fix and
  nothing happens.
- **`triggers.status_check` is `false`.** No GitHub check is published, so
  nothing appears in branch protection.

Both are per-repository settings in version-controlled `.diffuse/config.json`.
Turn them on before concluding that reviews are not working; see
`CONFIGURATION.md` in this bundle for the full reference.

### When nothing appears to happen

A webhook for a repository that has not been onboarded is refused with HTTP
409, because Diffuse only reviews repositories an operator has registered with
`repository add` above. This is the most common reason a correctly configured
webhook produces no reviews, and it is easy to misread as "the provider is not
delivering at all".

Refused deliveries are recorded, so you can tell the two apart:

```bash
docker compose exec db psql -U diffuse -d diffuse -c \
  "SELECT scm_provider, scm_base_url, repo_full_name, event_name,
          attempts, last_seen_at
     FROM scm_webhook_rejections
    ORDER BY last_seen_at DESC LIMIT 20;"
```

Rows matching the base URL being diagnosed mean GitHub *is* reaching Diffuse
and being turned away — onboard the repository and the next delivery will be
accepted. No matching rows alongside failures on GitHub's webhook delivery page
points at the ingress instead: TLS, DNS, the reverse proxy, or a
signature-secret mismatch. Each refusal is also logged as a
warning, so `docker compose logs app` shows them as they arrive.

## Backups and restore drills

PostgreSQL is the authoritative durable state. Repository mirrors are useful to
back up but can be fetched again from the SCM if their credentials and database
records survive.

Create a private backup directory and take a logical backup:

```bash
umask 077
mkdir -p backups
docker compose exec -T db \
  pg_dump -U diffuse -d diffuse --format=custom --no-owner --no-acl \
  > "backups/diffuse-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Copy backups off-host, encrypt them, apply a retention policy, and alert on a
missed backup. A backup is not trusted until it has been restored into a
disposable PostgreSQL 17 + pgvector database and verified there:

```bash
docker compose run --rm \
  -e DATABASE_URL=postgresql://diffuse:...@restore-host:5432/diffuse_restore \
  migrate database verify
```

`database verify` re-reads the applied migration ledger, compares every applied
version's SHA-256 against the packaged catalog, and confirms the version-1
baseline contract — tables, columns, and the `vector` extension. It exits
non-zero on checksum drift, a missing ledger, or a schema that does not match
the release. That is the whole trustworthiness check a restored dump can be
given from the runtime image, which ships no Python interpreter and no test
suite.

Rehearse the cutover as well: point a disposable stack at the restored database
and require `curl --fail http://127.0.0.1:8000/ready` to return `200`, which is
the same readiness gate the live `app` container uses.

Never test a restore over the live database. Record the exact restore procedure
for the server's backup system and rehearse it before the first production
upgrade.

## Upgrades

For every upgrade:

1. read the migration notes and take a verified off-host backup;
2. retrieve the new bundle with
   `oras pull ghcr.io/intuitumxyz/diffuse-self-host:vX.Y.Z`, check it with
   `sha256sum --check diffuse-self-host.tar.gz.sha256`, and unpack it beside —
   not over — the running deployment. The same two files are also attached to
   the tagged GitHub Release;
3. copy the new `compose.yaml` into place and carry your existing `.env`
   forward, taking only the new release's `DIFFUSE_IMAGE` digest from its
   `env.example`;
4. verify the image signature and pull the digest without stopping the existing
   stack, following the verification commands in the bundle's `README.md`;
5. run `docker compose --env-file .env up -d`; the one-shot migrator gates
   application startup;
6. require `docker compose run --rm migrate database verify` and a successful
   `/ready` response;
7. inspect `docker compose logs migrate app worker` for restarts or failed
   jobs; and
8. if the release notes say the index format changed, reindex every repository
   (see below).

Applied migration files are immutable. If verification reports checksum drift
or an unversioned schema, stop and investigate rather than bypassing the gate.

### Rolling back

**Redeploying the previous image does not roll back an upgrade that ran a
migration.** There are no down-migrations. Once the database records a version
newer than the build, the older build refuses to run: it raises
`MigrationDriftError` ("Database migration version is newer than this Diffuse
build"), the API fails its healthcheck, the worker exits, and `/ready` returns
503. This is deliberate — a build operating on a schema it does not understand
is worse than a build that will not start — but it means the previous image is
not a rollback path.

The only supported reversal is restoring the pre-upgrade backup, which is why
step 1 of every upgrade is a *verified* off-host backup and why the restore
drill above is not optional. To roll back:

1. stop the stack: `docker compose --env-file .env down`;
2. restore the pre-upgrade dump into a fresh database, following "Backups and
   restore drills" above — never over the live volume;
3. point `.env` back at the previous `DIFFUSE_IMAGE` digest and the restored
   database; and
4. start the stack and require `database verify` plus a `/ready` response.

Everything written after the backup is lost, which for Diffuse means reviews,
findings, feedback, and learned-rule approvals in that window. Indexes and
mirrors are rebuildable — reindex with `repository sync --all`.

Because rollback costs a restore, prefer to test an upgrade against a copy of
production first. The restore drill already produces exactly that copy.

### Upgrades that change the index format

Some releases change `INDEX_FORMAT_VERSION` — a language-adapter schema change,
a Tree-sitter grammar upgrade, or a repository-policy schema change. Index
snapshots are immutable and are matched on that format, so **every existing
snapshot becomes incompatible and no repository re-extracts on its own.**

Diffuse fails closed rather than reviewing without context: an affected
repository's reviews raise `MissingRepositoryIndexError`, retry, and then
dead-letter. Queue the sweep as the last step of such an upgrade:

```bash
docker compose run --rm worker repository sync --all
```

It prints one line per repository, is safe to re-run, and exits non-zero if any
repository could not be queued. Reviews for a repository resume once its new
snapshot is active, so expect a gap on large repositories and schedule the
upgrade window accordingly. Watch `docker compose logs worker` for progress.

## Initial operational checklist

- Exercise one test repository end to end before adding private production
  repositories.
- Provision repository-scoped service tokens with
  `docker compose run --rm worker token add`, which
  mints a high-entropy credential and prints it once, and reserve
  `DIFFUSE_API_TOKEN` for bootstrap/recovery.
- Keep `GITHUB_ALLOWED_INSTANCES` narrow.
- Monitor `/ready`, PostgreSQL disk growth, Docker volume capacity, container
  restart counts, worker errors, and backup age.
- Patch the host and install supported signed Diffuse releases regularly.
- Keep source execution disabled; Diffuse's current review path reads source
  and calls model/SCM APIs but is not a sandbox for running pull-request code.
