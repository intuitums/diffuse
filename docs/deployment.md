# Single-server deployment

This profile is for one trusted operator or a small trusted team on a Linux
server. Diffuse is still a foundation release: it does not yet provide
multi-tenant isolation, encrypted per-installation SCM credentials, operational
metrics, signed release images, or automated backup retention. Do not expose it
as an untrusted multi-tenant service.

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
cp .env.example .env
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

Do not commit `.env`, copy it into an image, or place its values on command
lines. Back it up separately in an encrypted secret manager.

Validate interpolation before starting anything:

```bash
docker compose config --quiet
```

Compose refuses to start without a database password, bootstrap token, and
public URL.

## Start and expose the service

Build and start the migration-gated stack:

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/ready
```

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
2. build the new image without stopping the existing stack;
3. run `docker compose up -d`; the one-shot migrator gates application startup;
4. require `docker compose run --rm migrate diffuse database verify` and a
   successful `/ready` response; and
5. inspect `docker compose logs migrate app worker` for restarts or failed
   jobs.

Applied migration files are immutable. If verification reports checksum drift
or an unversioned schema, stop and investigate rather than bypassing the gate.

## Initial operational checklist

- Exercise one test repository end to end before adding private production
  repositories.
- Provision repository-scoped service tokens and reserve
  `DIFFUSE_API_TOKEN` for bootstrap/recovery.
- Keep the SCM instance allowlists narrow.
- Monitor `/ready`, PostgreSQL disk growth, Docker volume capacity, container
  restart counts, worker errors, and backup age.
- Patch the host and rebuild the containers regularly.
- Keep source execution disabled; Diffuse's current review path reads source
  and calls model/SCM APIs but is not a sandbox for running pull-request code.
