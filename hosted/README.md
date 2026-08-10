# GitHub Integration Service

This directory is the intentionally small service deployed at
`https://api.diffuse.website`. It is not a hosted Diffuse review service.

It does only four jobs:

1. verifies the owner who returns from the Diffuse GitHub App connection flow;
2. accepts and HMAC-verifies the GitHub App's global webhook deliveries;
3. leases each installation's delivery to its enrolled self-hosted Diffuse
   instance over an outbound pull connection; and
4. brokers short-lived GitHub installation tokens to that same instance.

It must not receive source checkouts, repository mirrors, review findings,
model credentials, runner credentials, or review output. GitHub's event payload
is retained only until the self-hosted instance acknowledges it; an exceptional
undelivered payload is pruned on subsequent ingress after 30 days.

## Required Vercel environment

Set these as encrypted Production environment variables on the `diffuse`
Vercel project:

```dotenv
DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL=https://api.diffuse.website
# Required only for a non-Neon database; a Vercel-attached Neon database
# supplies DATABASE_URL automatically.
DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL=postgresql://...
DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER=...                  # base64url, at least 32 random bytes
DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK=...               # base64url, exactly 32 random bytes
# Optional dual-read key during rotation:
# DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS=...
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY=...                      # Diffuse GitHub App PEM; literal \n is accepted
GITHUB_WEBHOOK_SECRET=...
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
```

`DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER` hashes connection and instance credentials before
they reach the database. Generate it once with
`openssl rand -base64 48 | tr '+/' '-_' | tr -d '='`; preserve it for the life
of the database or every stored credential becomes invalid.

`DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK` seals reversible secrets such as
per-instance delivery signing keys with AES-GCM before they are stored.
Generate it with
`head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '='`.
During rotation, keep the old value in
`DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS` until every row has been
rewritten under the new key.

The OAuth application's callback URL is:

```text
https://api.diffuse.website/auth/github/callback
```

The GitHub App's global webhook URL is:

```text
https://api.diffuse.website/webhook/github
```

The App needs only the permissions required by Diffuse review: Contents read,
Pull requests read/write, Issues read when comment-triggered reviews are
enabled, and Checks write when checks are enabled. Subscribe it to `push`,
`pull_request`, `issue_comment`, and `pull_request_review_comment`, plus
`installation` so uninstall/suspend can revoke delivery. The self-hosted
delivery poller admits the same event set as the standalone `/webhook/github`
path.

## Database and deployment

Provision a dedicated PostgreSQL database for this service. The Vercel
Marketplace Neon integration supplies `DATABASE_URL` automatically; an operator
using another provider supplies `DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL` instead.

### Schema: keep upgrades additive

| Situation | File to run |
| --- | --- |
| **Existing production Neon** (already has tables) | `github_integration/migrations/001_connect_sessions.sql` only |
| Brand-new empty database | `github_integration/vercel_schema.sql` (or `python -m github_integration.migrate`) |

Do **not** re-run a rewritten `CREATE TABLE IF NOT EXISTS` bootstrap against a
live database and expect Postgres to reshape columns — it will not. Production
changes go in numbered files under `migrations/`.

### Apply on Neon (production today)

Vercel/Neon have no API from this agent that can execute SQL for you. Apply the
additive migration in the Neon SQL Editor:

1. Open [Vercel Dashboard](https://vercel.com) → project **diffuse** → **Storage**
   → the attached Neon database → **Open in Neon** (or neon.tech → that project).
2. Open **SQL Editor**.
3. Paste the full contents of
   `hosted/github_integration/migrations/001_connect_sessions.sql`.
4. Run it once. It is idempotent (safe to re-run).
5. Verify:

```sql
SELECT column_name, is_nullable
FROM information_schema.columns
WHERE table_name = 'setup_oauth_states'
  AND column_name IN ('installation_id', 'connect_session_id')
ORDER BY column_name;

SELECT to_regclass('public.connect_sessions') AS connect_sessions;
```

Expect `connect_session_id` present, `installation_id` nullable (`YES`), and
`connect_sessions` non-null.

Then deploy the `hosted/` tree. Do not deploy the new connect routes before this
migration lands.

### Local / scripted apply

```bash
cd hosted
# Fresh DB only:
python -m github_integration.migrate
# Existing DB: run migrations/001_connect_sessions.sql via psql against DATABASE_URL
```

Deploy this directory as the Vercel project root. `api/index.py` exports the
FastAPI ASGI application and `vercel.json` rewrites all API paths to it.

`GET /health` is intentionally configuration-free. It proves the deployment is
reachable but does not claim that GitHub, OAuth, or PostgreSQL has been
configured. Validate those by installing the Diffuse GitHub App once, then
running `diffuse github connect` from a self-hosted instance.

## Operational limits

The first deployment leases at most 20 events per pull and waits 15 seconds
between empty polls. With 200 enrolled instances, that is roughly 13 small
requests per second while idle—not a workload that requires a dedicated
always-on server. PostgreSQL is the durable queue; Vercel functions do only
short verification, storage, leasing, and token-broker requests.
