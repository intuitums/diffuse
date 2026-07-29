# Integration relay deployment

The Diffuse Integration Relay is the small public service behind the shared
GitHub App. It receives and buffers provider callbacks and brokers short-lived
installation credentials. It does not run a review worker, mount a repository
volume, call models, or expose the review API. Raw callback bodies can include
repository metadata and human-authored text; they are erased when the paired
node acknowledges local ingestion.

Customer Diffuse nodes connect to it over outbound HTTPS. This service should
run on managed infrastructure with a durable PostgreSQL database; a residential
server and its UPS are not an appropriate availability boundary for shared App
callbacks.

## Public endpoints

Configure one HTTPS origin, such as
`https://integrations.diffuse.example`, with:

| Purpose | URL |
| --- | --- |
| Authenticate GitHub App installer | `/auth/github` |
| GitHub installation-auth callback | `/auth/github/callback` |
| GitHub App setup URL | `/setup` |
| GitHub webhook | `/webhook/github` |
| Node API | `/relay/v1/*` |
| Liveness/readiness | `/health`, `/ready` |

The GitHub App needs **Contents: read**, **Pull requests: read and write**,
**Issues: read**, and **Checks: read and write** when checks are enabled.
Subscribe to `installation`, `push`, `pull_request`, `issue_comment`, and
`pull_request_review_comment`. Set a high-entropy webhook secret.

The signed `installation.created` event is authoritative for who installed the
App. The setup callback independently verifies the installation through the
GitHub App API and will not issue a pairing code until the signed event and
authenticated GitHub user agree.

## Secrets

Copy `deploy/gateway.env.example` to a secret-manager-rendered `gateway.env`.
Generate `POSTGRES_PASSWORD` and `GITHUB_WEBHOOK_SECRET` independently. Mount:

- the GitHub App private key at
  `/run/secrets/diffuse/github-app.pem`; and
- the GitHub App OAuth client secret at
  `/run/secrets/diffuse/github-oauth-client-secret`.

Set `GITHUB_APP_ID` to the App client ID or numeric App ID,
`GITHUB_OAUTH_CLIENT_ID` to its client ID, `GITHUB_APP_SLUG` to its public slug,
and `DIFFUSE_PUBLIC_URL` to the exact public HTTPS origin.
`GITHUB_APP_INSTALLATION_ID` must stay empty because each paired node selects
its own installation.

Pairing codes and node credentials are stored only as SHA-256 digests. The App
private key and webhook secret exist only on the relay. For public operation,
move App signing to a sign-only KMS/HSM and retain the encrypted secret manager
as the credential home of record.

## Start

The supplied profile runs only PostgreSQL, the migration job, and the gateway:

```bash
docker compose \
  --file deploy/gateway.compose.yaml \
  --env-file gateway.env \
  pull

docker compose \
  --file deploy/gateway.compose.yaml \
  --env-file gateway.env \
  up -d

curl --fail http://127.0.0.1:8000/ready
```

Terminate TLS at a managed load balancer or reverse proxy and forward to the
private gateway port. Back up PostgreSQL before every upgrade. Alert on failed
readiness, callback error rates, database capacity, undelivered queue age, and
node authentication failures.

The current migration catalog includes Diffuse's base schema and therefore
uses the pgvector PostgreSQL image even though the relay itself does not create
embeddings. There is no review worker or repository storage in this profile.

## Pair a node

An operator visits `https://integrations.diffuse.example/auth/github`, signs in,
installs the App, and receives a command with a ten-minute one-time code:

```bash
diffuse relay pair \
  --gateway https://integrations.diffuse.example \
  --code <single-use-code>
```

The response contains a node token exactly once. Store it as
`DIFFUSE_RELAY_TOKEN` with the returned `gateway_url` as `DIFFUSE_RELAY_URL` on
the customer node. Re-pairing the installation rotates the prior node token.

The GitHub MVP binds one active node to one App installation. Slack callback
delivery, multi-installation nodes, node failover, queue retention automation,
and fleet SLO dashboards are explicit follow-up work.
