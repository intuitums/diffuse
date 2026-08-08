# Deploying Diffuse v1

> **Current status:** guided GitHub App connection is the next v1 delivery
> item (DEV-348). Until it lands, the manual App credentials below are the
> temporary operator setup. There is no Diffuse user login, browser OAuth,
> public REST API, service token, or public MCP configuration.

Diffuse is a self-hosted GitHub pull-request reviewer. Run its API, worker,
PostgreSQL database, repository mirrors, and isolated CLI review runners inside
infrastructure you control.

## Prerequisites

- Docker Engine with Compose
- a public HTTPS URL for Diffuse; GitHub must be able to deliver webhooks to
  `https://your-diffuse-host/webhook/github`
- a GitHub App installed only on the repositories to review
- PostgreSQL storage and persistent disk for repository mirrors
- a configured review engine or runner credentials appropriate to the active
  transitional runtime

For local development, use an explicit webhook proxy. GitHub cannot deliver to
plain `localhost`.

## Temporary manual GitHub App setup

Create a private App owned by the organization that owns the repositories.
Give it only the permissions Diffuse needs:

- **Contents: read**
- **Pull requests: read and write**
- **Issues: read** when using comment-triggered reviews
- **Checks: read and write** when publishing a Check

Subscribe it to `push` and `pull_request`; add `issue_comment` and
`pull_request_review_comment` only when those review interactions are enabled.
Set its webhook URL to `https://your-diffuse-host/webhook/github`, generate a
high-entropy webhook secret, and install it on the intended repositories.

Copy `.env.example` to `.env`, then configure only the GitHub values required
by the current foundation:

```dotenv
DIFFUSE_PUBLIC_URL=https://your-diffuse-host
GITHUB_APP_ID=...
GITHUB_APP_INSTALLATION_ID=...
GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/diffuse-github-app.pem
GITHUB_WEBHOOK_SECRET=...
```

Keep the private key and webhook secret in your deployment secret manager (or
a read-only `600` file mount), never in source control. Diffuse uses the App
key to mint short-lived installation tokens; do not supply an installation
token by hand. `GITHUB_TOKEN` is a compatibility fallback, not the preferred
path.

## Start and verify

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD, GitHub App credentials, and the active review runtime.
docker compose up -d --build
curl --fail https://your-diffuse-host/health
curl --fail https://your-diffuse-host/ready
```

Use a separate secret-managed value for `POSTGRES_PASSWORD`; changing it after
the database initializes also requires changing the PostgreSQL role password.

The public service exposes only GitHub webhook ingress plus `/health` and
`/ready`. `/mcp`, `/api/v1`, `/docs`, browser login routes, and automatic
approval are intentionally absent.

The upcoming guided setup will replace the manual App-registration and
installation steps with a verified, operator-owned GitHub App flow. See
[v1-scope.md](v1-scope.md) for the active delivery order.
