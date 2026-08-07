# Single-server deployment

This profile is for one trusted operator or a small trusted team on a Linux
server. Diffuse is still a foundation release: it does not yet provide
multi-tenant isolation, encrypted per-installation SCM credentials, operational
metrics, or automated backup retention. Do not expose it as an untrusted
multi-tenant service.

## Host preparation

Provide:

- a current Docker Engine with the Compose plugin;
- a DNS name whose HTTPS traffic terminates at a reverse proxy;
- outbound HTTPS access to GitHub and the configured model provider;
- enough persistent disk for PostgreSQL and repository mirrors; and
- host-level monitoring for disk, memory, container restarts, and backup age.

Only SSH and the reverse proxy's HTTP/HTTPS ports should be public. Compose
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

- `DIFFUSE_PUBLIC_URL=https://diffuse.example.com`;
- `DIFFUSE_MCP_ALLOWED_HOSTS=diffuse.example.com`;
- the GitHub integration, including a high-entropy webhook secret;
- the GitHub web/API origins and the narrowest possible
  `GITHUB_ALLOWED_INSTANCES`; and
- the model credentials or self-hosted model endpoint.

Every configured origin ends up carrying a token or a clone credential, so
`http://` is refused for anything other than loopback.
`DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1` lifts that for a lab instance and should
never be set in production.

Do not commit `.env`, copy it into an image, or place its values on command
lines. Back it up separately in an encrypted secret manager.

### Configure the GitHub App

Diffuse authenticates automation as a GitHub App installation. Do not mint an
installation token by hand and do not create a PAT: installation tokens expire
after one hour, and Diffuse creates and refreshes them itself.

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
The migration container does not need the key. A file-backed key is re-read
when the in-memory installation token refreshes, but recreate `app` and
`worker` when rotating it so the new key is exercised immediately.

`GITHUB_TOKEN` remains only as a compatibility fallback. Leave it empty for
App authentication. A partial App configuration fails closed rather than
silently falling back to a different identity.

GitHub permits overlapping App private keys. Rotate without downtime by
generating a second key, replacing the secret-manager value, recreating app and
worker, confirming a token can be minted, and only then deleting the old key.

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

Two properties of Diffuse constrain how rotation works:

- **The process reads its credentials from its own environment.** A rotated
  value does not reach a running container; re-render and recreate the
  containers. The one exception is the browser sign-in client secret, which is
  read from a file per call — and that flow is disabled, so it does not help
  here.
- **`POSTGRES_PASSWORD` is not rotatable by editing the file.** Compose
  interpolates it into `DATABASE_URL`, and PostgreSQL stores the password in the
  data volume on first initialization. Changing it later requires an
  `ALTER ROLE` against the running database as well. Choose it before the first
  `up`.

Rotating `GITHUB_WEBHOOK_SECRET` has a delivery gap: the server verifies against
exactly one secret, so deliveries signed with the old value are rejected from
the moment the new one is live until GitHub's webhook configuration is updated.
Rotate it during a quiet period and re-deliver anything GitHub records as
failed.

Whatever the source of the values, the security property that matters is that
the credential's home of record is somewhere with access control, an audit
trail, and rotation — not a file that exists only on one host. `.env` on the
server is a cache of that record, not the record itself.

### Model credentials without a long-lived key

Retrieval is graph and lexical search inside PostgreSQL, so indexing needs no
model credential. CLI-native review needs only the vendor-managed runner
credentials; transitional `REVIEW_RUNTIME=litellm` additionally needs the
credential its `REVIEW_MODEL` resolves. Running Diffuse with no OpenAI account
is therefore just a matter of not naming an OpenAI LiteLLM model or logging in
the Codex runner.

For the cloud providers, ambient credential chains work and are preferable to a
stored key: `vertex_ai/…` resolves Google application default credentials and
applies to `REVIEW_MODEL`. `REVIEW_API_BASE` points review generation
at a self-hosted OpenAI-compatible endpoint, which needs no managed provider
credential either.

Two questions come up often enough to answer directly, both about OpenAI
specifically.

**Can Diffuse sign in with a ChatGPT account instead of an API key?** No, and
not because Diffuse has not implemented it. The OpenAI Platform API has no
authorization-code flow that mints platform access on a user's behalf;
"Sign in with ChatGPT" covers apps *inside* ChatGPT and the Codex CLI, which is
an interactive desktop session, not a headless worker. There is nothing for
Diffuse to call.

**Can it use OpenAI workload identity federation?** That is the right shape —
a workload exchanges an OIDC token from AWS, GCP, Azure, Kubernetes, or GitHub
Actions for a short-lived OpenAI token, with no stored key — and the OpenAI
Python SDK accepts a `workload_identity` client parameter. Diffuse cannot reach
it today: every model call goes through LiteLLM, and LiteLLM has no passthrough
for that parameter and no way to inject a pre-built client. The blocker is
upstream.

### Browser sign-in is not usable yet

Leave `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET_FILE`, and
`GITHUB_APP_SLUG` unset, and do not create a secrets directory for them.

The server half of GitHub browser sign-in is mounted — `/auth/cli`,
`/auth/github/callback`, and `/setup` exist and will mint a session — but
nothing consumes the result. There is no `diffuse login` command; the CLI's
subcommands are `review`, `repository`, `cluster`, `learning`, `token`,
`database`, `evaluate`, and `model`. No request authenticator reads a session:
the API, MCP, and REST surfaces accept only `DIFFUSE_API_TOKEN` or a
repository-scoped service token. Configuring OAuth today therefore places a
GitHub client secret on the host and grants no access in return.

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

Terminate TLS in a reverse proxy on the same host. For example, a minimal Caddy
site is:

```caddyfile
diffuse.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Point the GitHub webhook at
`https://diffuse.example.com/webhook/github`. Enable exactly these four event
types and no others:

- `push`
- `pull_request`
- `issue_comment`
- `pull_request_review_comment`

Verify a signed `ping` or harmless test delivery before onboarding production
repositories.

## Agent CLI credentials

The agent credential is a vendor-managed OAuth credential, not a Diffuse API
key. The release profile stores each vendor credential in its own
`claude_agent_data` or `codex_agent_data` named volume at
`/var/lib/diffuse/agent`, owned by uid/gid **10001** with mode **0700**. The
root filesystem is read-only and `/tmp` is ephemeral, so do not redirect
`DIFFUSE_AGENT_HOME` to either location.

Only `agent-runner-claude` (profile `agent-claude`) and `agent-runner-codex`
(profile `agent-codex`) mount their respective volumes. The worker and API
never receive agent credentials and never execute a CLI — see
[agent-runtimes.md](agent-runtimes.md). Vendor CLIs refresh their credentials
in place, so exactly one writer exists per vendor.

The API and worker also need a separate, high-entropy
`DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY` before a runner session can use the
private capability-tool surface. The runner never receives that key: it only
presents a short-lived signed capability. Generate it with
`openssl rand -base64 48` and keep it in `.env`, alongside—not in—the
`agent_data` volume.

Sign in through the long-lived runner (the command overrides its daemon entry
point for this one operator action):

```bash
docker compose --profile agent-codex run --rm agent-runner-codex agent login codex --device-auth
docker compose --profile agent-claude run --rm agent-runner-claude agent login claude --console
```

The runner sets `HOME` to a private subdirectory of the credential volume, and
the login command sets the vendor's explicit config variable on top of it. This
keeps vendor fallback state such as a legacy home-directory auth file off the
read-only image. That subdirectory is created in the image, so it exists on a
stack that has never signed in to an agent. To rotate or decommission the
credential, use the matching vendor logout in the same context, then sign in
again if needed:

```bash
docker compose --profile agent-claude run --rm agent-runner-claude agent logout claude
```

Do **not** back up either agent credential volume. It is a live, revocable vendor credential, not
authoritative Diffuse state; including it in a backup multiplies a long-lived
refresh token. Recreate it by running the login command again after a restore.

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

### No status check is published by default

**`triggers.status_check` is `false`**, so no GitHub check appears and nothing
shows up in branch protection. It is a per-repository setting in
version-controlled `.diffuse/config.json`; turn it on before concluding that
reviews are not working, and see `CONFIGURATION.md` in this bundle for the full
reference.

### Every push to an open pull request costs a model call

`triggers.review_updates` is `true`, so pushing further commits to an open pull
request re-reviews it — which is what makes finding lineage and
addressed-detection work, and what makes each push cost money.
`REVIEW_UPDATE_DEBOUNCE_SECONDS` (default `60`) bounds that: a pushed revision
waits before the worker may claim it, so a burst of pushes collapses into a
single review of the final head. The wait is measured from the first push of the
burst, so a steady drip of commits cannot defer the review indefinitely, and it
never delays a review that was already due. Set it to `0` to review every push
immediately, or raise it on a busy installation.

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
disposable PostgreSQL 17 database and verified there:

```bash
docker compose run --rm \
  -e DATABASE_URL=postgresql://diffuse:...@restore-host:5432/diffuse_restore \
  migrate database verify
```

`database verify` re-reads the applied migration ledger, compares every applied
version's SHA-256 against the packaged catalog, and confirms the version-1
baseline contract — every table and column the frozen version-1 schema
declares, minus those a later migration deliberately drops. It exits
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

### Agent-volume ownership change

This release pins the container `diffuse` account to uid/gid 10001 so the
credential volume has a stable host-visible owner. Existing
`repository_data` volumes created by older images can retain the old numeric
owner. Before starting this release, run this one-time repair while the stack
is stopped (replace `diffuse_repository_data` with your Compose project's actual
volume name):

```bash
docker run --rm -v diffuse_repository_data:/data alpine:3.22 \
  chown -R 10001:10001 /data
```

Do not apply this command to `agent_data`; a new empty agent volume receives
the image directory's uid, gid, and mode automatically.

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
