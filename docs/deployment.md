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

Install the Diffuse GitHub App on the organization or user that owns the
repositories you want reviewed (most operators do this first). Then, on the
machine that runs self-hosted Diffuse, connect that installation:

```bash
diffuse github connect
diffuse github status
```

The CLI opens a browser, asks you to authorize GitHub, and — when you already
have the App installed — binds that installation and writes credentials to
`./github-integration.env` (mode 0600). The instance label defaults to this
machine's hostname. If the App is not installed yet, the browser page links to
the install flow and returns to the same CLI session afterward.

Credentials are returned exactly once; load that env file (or copy the values
into your deployment's secret store). The required values are:

```dotenv
DIFFUSE_GITHUB_INTEGRATION_URL=https://api.diffuse.website
DIFFUSE_GITHUB_INTEGRATION_TOKEN=...
DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY=...
```

`diffuse github status` reports ready/not-ready against the Integration Service
(installation active, pending deliveries). `diffuse github disconnect` revokes
the instance credential when retiring a deployment.

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
docker compose --profile agent-claude --profile agent-codex up -d --build
curl --fail https://your-diffuse-host/health
curl --fail https://your-diffuse-host/ready
```

The Agent Host services are profile-gated: without at least the profile
matching `REVIEW_AGENT`, `up` starts no Agent Host and the worker fails its
startup validation.

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
