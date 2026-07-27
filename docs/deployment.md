# Single-server deployment

This profile is for one trusted operator or a small trusted team on a Linux
server. Diffuse is still a foundation release: it does not yet provide
multi-tenant isolation, encrypted per-installation SCM credentials, operational
metrics, connected/offline entitlement enforcement, or automated backup
retention. Do not expose it as an untrusted multi-tenant service.

## Host preparation

Provide:

- a current Docker Engine with the Compose plugin;
- a DNS name whose HTTPS traffic terminates at a reverse proxy;
- outbound HTTPS access to the selected SCM and model provider;
- enough persistent disk for PostgreSQL and repository mirrors; and
- host-level monitoring for disk, memory, container restarts, and backup age.

Only SSH and the reverse proxy's HTTP/HTTPS ports should be public. Compose
binds PostgreSQL and Diffuse itself to `127.0.0.1`; keep those bindings private.

## Configure secrets

Create the deployment environment and restrict it to the operator:

```bash
# Customer release bundle:
cp env.example .env
# Private source workspace instead:
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
- exactly one or both SCM integrations, including a high-entropy webhook
  secret/signing token;
- the applicable SCM web/API origins and the narrowest possible additional
  instance allowlist; and
- the model credentials or self-hosted model endpoint.

Every configured origin ends up carrying a token, a clone credential, or the
OAuth client-secret exchange, so `http://` is refused for anything other than
loopback. `DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1` lifts that for a lab instance and
should never be set in production.

Do not commit `.env`, copy it into an image, or place its values on command
lines. Back it up separately in an encrypted secret manager.

### Browser sign-in (optional)

`diffuse login` signs in through GitHub. Diffuse is the confidential OAuth
client, so the GitHub App client secret is read from a file rather than the
environment and never belongs in `.env`:

```bash
sudo install -d -m 0711 /srv/diffuse/secrets
sudo install -m 600 /dev/stdin /srv/diffuse/secrets/app-client-secret
sudo chown "$(docker compose run --rm --no-deps --entrypoint id app -u)" \
    /srv/diffuse/secrets/app-client-secret
```

Paste the client secret on stdin, then end with Ctrl-D.

Both permissions matter, and they grant the narrowest access that works. The
container runs as an unprivileged user, so the `chown` is what lets it read the
file at all. The directory is `0711` — traversable but **not** listable — rather
than `0700`, because a root-owned `0700` directory blocks that user from
reaching the file even when the file itself is chowned to them. Sibling secrets
in the same directory, such as an `app-private-key.pem`, stay unreadable to the
container because they keep their own `0600` root ownership. If the directory
already exists at `0700`, `sudo chmod 0711` it.

Compose mounts the directory read-only; override the host path with
`DIFFUSE_SECRETS_DIR` if you keep secrets elsewhere.

Then set `GITHUB_OAUTH_CLIENT_ID` and, to offer an install link after sign-in,
`GITHUB_APP_SLUG`. In the GitHub App set the callback URL to
`https://diffuse.example.com/auth/github/callback` and the setup URL to
`https://diffuse.example.com/setup`. Sign-in stays disabled and returns 503
until the client id and a readable secret are both present; Diffuse logs a
warning if the secret file is group- or world-readable and refuses to read it
at all if it is group- or world-writable.

Validate interpolation before starting anything:

```bash
docker compose config --quiet
```

Compose refuses to start without a database password, bootstrap token, and
public URL.

## Start and expose the service

Pull and start a customer release bundle's digest-pinned, migration-gated
stack:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
docker compose ps
curl --fail http://127.0.0.1:8000/ready
```

Maintainers working from the private source workspace instead use
`docker compose up -d --build`.

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

Point the provider webhook at
`https://diffuse.example.com/webhook/github` or
`https://diffuse.example.com/webhook/gitlab`. Enable only the webhook event
types described in the main README, and verify a signed `ping` or harmless test
delivery before onboarding production repositories.

### When nothing appears to happen

A webhook for a repository that has not been onboarded is refused with HTTP
409, because Diffuse only reviews repositories an operator has registered with
`diffuse repository add`. This is the most common reason a correctly configured
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

Rows matching the provider and base URL being diagnosed mean that SCM *is*
reaching Diffuse and being turned away — onboard the repository and the next
delivery will be accepted. No matching rows alongside failures in that
provider's webhook delivery page points at the ingress instead: TLS, DNS, the
reverse proxy, or a signature-secret mismatch. Each refusal is also logged as a
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
disposable PostgreSQL 17 + pgvector database and these commands succeed:

```bash
DATABASE_URL=postgresql://... diffuse database verify
POSTGRES_TEST_DATABASE_URL=postgresql://... pytest -m integration
```

Never test a restore over the live database. Record the exact restore procedure
for the server's backup system and rehearse it before the first production
upgrade.

## Upgrades

For every upgrade:

1. read the migration notes and take a verified off-host backup;
2. obtain the new signed bundle, verify its image signature, and pull its
   digest without stopping the existing stack;
3. run `docker compose --env-file .env up -d`; the one-shot migrator gates
   application startup;
4. require `docker compose run --rm migrate database verify` and a
   successful `/ready` response; and
5. inspect `docker compose logs migrate app worker` for restarts or failed
   jobs; and
6. if the release notes say the index format changed, reindex every repository
   (see below).

Applied migration files are immutable. If verification reports checksum drift
or an unversioned schema, stop and investigate rather than bypassing the gate.

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
- Provision repository-scoped service tokens with `diffuse token add`, which
  mints a high-entropy credential and prints it once, and reserve
  `DIFFUSE_API_TOKEN` for bootstrap/recovery.
- Keep the SCM instance allowlists narrow.
- Monitor `/ready`, PostgreSQL disk growth, Docker volume capacity, container
  restart counts, worker errors, and backup age.
- Patch the host and install supported signed Diffuse releases regularly.
- Keep source execution disabled; Diffuse's current review path reads source
  and calls model/SCM APIs but is not a sandbox for running pull-request code.
