# Deploying Diffuse

Diffuse is a self-hosted GitHub pull-request reviewer. Run its API, worker,
PostgreSQL database, repository mirrors, and isolated CLI review runners inside
infrastructure you control.

## Prerequisites

- Docker Engine with Compose
- outbound HTTPS access from Diffuse to `https://api.diffuse.website`
- the **Diffuse GitHub App** installed on the repositories to review
- PostgreSQL storage and persistent disk for repository mirrors
- `REVIEW_AGENT=codex` or `REVIEW_AGENT=claude`, Agent Dispatch keys, a Review
  Access Grant signing key, and signed-in matching Agent Hosts

The self-hosted instance does not need a public domain or an inbound webhook
port. GitHub delivers to the Diffuse GitHub App; the instance polls its signed
deliveries over its existing outbound HTTPS connection.

## Connect the Diffuse GitHub App

Install the Diffuse GitHub App. Its callback opens the GitHub connection flow,
where an organization owner confirms that they control the installation. The
flow shows a single-use connection code.

On the machine that runs self-hosted Diffuse, claim that code:

```bash
diffuse github connect '<one-time-code>' --name 'production-reviewer' \
  --write-env /etc/diffuse/github-integration.env
```

Prefer `--write-env` (mode 0600) over printing secrets to stdout. The command
returns credentials exactly once; set them in the self-hosted deployment's secret-managed
environment. The required values are:

```dotenv
DIFFUSE_GITHUB_INTEGRATION_URL=https://api.diffuse.website
DIFFUSE_GITHUB_INTEGRATION_TOKEN=...
DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY=...
```

`DIFFUSE_GITHUB_INTEGRATION_TOKEN` identifies only that self-hosted instance. It
lets it pull its installation's events and request a short-lived GitHub
installation token. The shared App private key, App webhook secret, source
code, mirrors, findings, Agent credentials, and review output never leave the
self-hosted deployment.

## Start and verify

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD, GitHub integration credentials, REVIEW_AGENT, and the
# Agent Dispatch / Review Access Grant keys.
docker compose up -d --build
curl --fail https://your-diffuse-host/health
curl --fail https://your-diffuse-host/ready
```

Use a separate secret-managed value for `POSTGRES_PASSWORD`; changing it after
the database initializes also requires changing the PostgreSQL role password.

The self-hosted API exposes `/health` and `/ready`. Its legacy direct GitHub
webhook endpoint is only for the advanced standalone-App configuration; do not
configure it when using the Diffuse GitHub App.

## Advanced: operate your own GitHub App

The local `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, App private key, and
`GITHUB_WEBHOOK_SECRET` settings remain for organizations that deliberately
operate their own App and publicly route webhooks to their Diffuse instance.
They must not be configured alongside the GitHub Integration Service credentials.
