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
- the GitHub integration, including a high-entropy webhook secret;
- the GitHub web/API origins and the narrowest possible additional instance
  allowlist; and
- the model credentials or self-hosted model endpoint.

Every configured origin ends up carrying a token or a clone credential, so
`http://` is refused for anything other than loopback.
`DIFFUSE_ALLOW_PLAINTEXT_ORIGINS=1` lifts that for a lab instance and should
never be set in production.

Do not commit `.env`, copy it into an image, or place its values on command
lines. Back it up separately in an encrypted secret manager.

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
`diffuse token add` service tokens for routine clients.

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

Point the GitHub webhook at
`https://diffuse.example.com/webhook/github`. Enable only the webhook event
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
   not over — the running deployment. The registry is the customer channel: it
   needs only the GHCR credential already issued for the `diffuse` image. The
   same two files are also attached to the tagged GitHub Release, but that page
   requires read access to the private source repository, so it is the
   staff and support channel rather than the customer one;
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
