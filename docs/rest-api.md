# REST API

Diffuse exposes a versioned control-plane API under `/api/v1`. It uses the same
bootstrap credential, hashed service-token lifecycle, repository grants, and
PostgreSQL projections as the [MCP server](mcp.md).

**The API documents itself.** The OpenAPI document is served at
`/openapi.json` and an interactive reference at `/docs`, both from the running
instance, so they are always correct for the version you are running. This page
does not restate the schema; it covers what the surface is for, how to
authorize a client, and the conventions the schema cannot express.

## Authorization

Provision a routine client with API-specific scopes:

```bash
diffuse token add dashboard-reader \
  --scope diffuse:api:read \
  --repository-id 1 \
  --actor operator
```

The token lifecycle — CSPRNG minting, one-time display, SHA-256-digest storage,
repository grants, revocation, and audit events — is
[the same as for MCP](mcp.md#minting-a-service-token).

| Operation | Required scopes |
| --- | --- |
| Reads, source search | `diffuse:api:read` |
| Repository Q&A | `diffuse:api:read` **and** `diffuse:api:generate` |
| Review trigger, reindex | `diffuse:api:read` **and** `diffuse:api:write` |
| Creating a repository | `diffuse:admin` **and** all-repositories access |

Add `diffuse:api:generate` only when the client may call model-backed
repository Q&A. Reindexing is further restricted by the token's repository
grant. Creating a repository requires all-repositories access because a
repository-specific grant cannot safely authorize an object that does not exist
yet.

## What v1 covers

The first v1 surface provides:

- repository list/detail with mirror and active immutable-index state;
- repository pull-request list/detail, review list/detail, and current finding
  list;
- exact half-open review analytics with optional repository and author filters;
- commit-pinned hybrid code search; and
- evidence-bounded repository Q&A with claim-level source citations;
- an authoritative GitHub pull-request review trigger that re-fetches the
  current open head before enqueueing durable work;
- administrative GitHub repository registration that validates the exact
  configured origin and clone access before queueing the resolved
  default-branch commit; and
- repository-authorized default-branch reindex requests using the same mirror,
  push-event ledger, and exact-commit worker path as authenticated webhooks.

## Conventions

All repository object paths use Diffuse's durable numeric repository ID. List
routes use bounded `limit`/`offset` pagination. Object lookups return the same
404 response for missing and unauthorized resources, and malformed requests
or authorization failures use `application/problem+json`.

## Idempotency

Every repository, index, or review mutation requires a client-generated
`Idempotency-Key` of 1–200 URL-safe characters. Diffuse stores only its SHA-256
digest, scopes it to the authenticated actor and operation, rejects reuse with
a different route or body, and replays the exact completed response. Repository
index operations persist the resolved exact-commit event before queueing, so a
crash retry cannot drift to a newer branch head. An in-progress request returns
409 with `Retry-After`; a replay includes `Idempotency-Replayed: true`.

## Examples

```bash
curl \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  "https://diffuse.example.com/api/v1/repositories?limit=20&offset=0"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"query":"where is tenant authorization enforced?","limit":8}' \
  "https://diffuse.example.com/api/v1/repositories/1/code/search"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Idempotency-Key: 5d47f272-c0d8-4c1f-a1df-b9561fb99d8f" \
  --header "Content-Type: application/json" \
  --data '{}' \
  "https://diffuse.example.com/api/v1/repositories/1/pull-requests/42/reviews"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_ADMIN_TOKEN}" \
  --header "Idempotency-Key: 890e7a82-43a6-485a-8c74-b7311517c90d" \
  --header "Content-Type: application/json" \
  --data '{
    "remote": "github",
    "remoteUrl": "https://github.com",
    "name": "owner/repository",
    "defaultBranch": "main"
  }' \
  "https://diffuse.example.com/api/v1/repositories"

curl \
  --request POST \
  --header "Authorization: Bearer ${DIFFUSE_CLIENT_TOKEN}" \
  --header "Idempotency-Key: a16df2a6-90bb-46c5-8c00-f894e23d7214" \
  --header "Content-Type: application/json" \
  --data '{}' \
  "https://diffuse.example.com/api/v1/repositories/1/indexes"
```
