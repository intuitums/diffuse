# Deploying Diffuse

Diffuse is a self-hosted GitHub pull-request reviewer. Run its API, worker,
PostgreSQL database, repository mirrors, and isolated CLI review runners inside
infrastructure you control.

## Prerequisites

- Docker Engine with Compose
- outbound HTTPS access from Diffuse to `https://api.diffuse.website`
- the shared **Diffuse-Agent** GitHub App installed on the repositories to review
- PostgreSQL storage and persistent disk for repository mirrors
- a configured review engine or runner credentials appropriate to the active
  transitional runtime

The self-hosted instance does not need a public domain or an inbound webhook
port. GitHub delivers to Diffuse-Agent; the instance pulls its own signed
deliveries over its existing outbound HTTPS connection.

## Connect the shared Diffuse-Agent App

Install the Diffuse-Agent GitHub App. Its callback opens the hosted setup flow,
where an organization owner confirms that they control the installation. The
flow shows a single-use enrollment code.

On the machine that runs self-hosted Diffuse, claim that code:

```bash
diffuse hosted enroll '<one-time-code>' --name 'production-reviewer'
```

It prints the credentials to set in the self-hosted deployment's secret-managed
environment. The required values are:

```dotenv
DIFFUSE_HOSTED_RELAY_URL=https://api.diffuse.website
DIFFUSE_HOSTED_TOKEN_BROKER_URL=https://api.diffuse.website
DIFFUSE_HOSTED_INSTANCE_TOKEN=...
DIFFUSE_HOSTED_EVENT_SIGNING_KEY=...
```

`DIFFUSE_HOSTED_INSTANCE_TOKEN` identifies only that self-hosted instance. It
lets it pull its installation's events and request a short-lived GitHub
installation token. The shared App private key, App webhook secret, source
code, mirrors, findings, model credentials, and review output never leave the
self-hosted deployment.

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

The self-hosted API exposes `/health` and `/ready`. Its legacy direct GitHub
webhook endpoint is only for the advanced standalone-App configuration; do not
configure it when using Diffuse-Agent.

## Advanced: operate your own GitHub App

The local `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, App private key, and
`GITHUB_WEBHOOK_SECRET` settings remain for organizations that deliberately
operate their own App and publicly route webhooks to their Diffuse instance.
They must not be configured alongside the hosted token broker.
