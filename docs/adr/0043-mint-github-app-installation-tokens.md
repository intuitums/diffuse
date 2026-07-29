# ADR 0043: Mint GitHub App installation tokens

Date: 2026-07-29

Status: Accepted; amended by ADR 0044. Standalone nodes retain the local App
identity described below. Relay nodes now use the hosted token broker selected
by ADR 0044, and the gateway selects an installation per paired node.

## Context

Diffuse authenticated every GitHub API and Git operation with one static
`GITHUB_TOKEN`. The environment examples described that value as either a
GitHub App installation token or a user access token, but only the user token
was operationally viable: GitHub App installation tokens expire after one hour
and Diffuse did not renew them.

That pushed a long-running automation service toward a credential attached to
a person, even though GitHub's installation identity is the intended actor.
It also contradicted the GitHub-only direction in ADR 0041, whose forcing
decision is per-installation App authentication.

The current self-hosted profile supports one trusted operator and one GitHub
organization. Full multi-tenant installation discovery and encrypted
database-backed private keys are not required to stop using a PAT internally.

## Alternatives considered

- **Keep accepting manually minted installation tokens.** Rejected because the
  operator would have to replace the token and recreate containers every hour.
  Calling that App support hides an outage timer.
- **Use a machine-user PAT.** Rejected as the preferred path because actions
  are attributed to a user, lifecycle depends on that account, and GitHub's own
  App guidance says automation acting independently of a user should use an
  installation token.
- **Adopt an external GitHub App token broker.** Rejected for the single-server
  profile. Signing one RS256 JWT and making one exchange per process per hour is
  smaller than another always-on service and another availability dependency.
- **Implement the complete multi-tenant installation schema now.** Deferred,
  not rejected. It requires threading installation identity through durable
  events, repositories, jobs, and every credential call site. One configured
  installation is sufficient for the internal pilot and does not pretend to
  solve that larger boundary.

## Decision

`service/github_app.py` is the process-level GitHub credential resolver.
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and a private key supplied by
`GITHUB_APP_PRIVATE_KEY_FILE` or `GITHUB_APP_PRIVATE_KEY` configure App
authentication.

The resolver signs an RS256 JWT, exchanges it at
`POST /app/installations/{id}/access_tokens`, caches the returned token only in
memory, and refreshes five minutes before its documented one-hour expiry. A
partial App configuration fails closed. `GITHUB_TOKEN` remains a
backward-compatible fallback only when no App setting is present.

Every GitHub REST, GraphQL, review-publication, and Git askpass call resolves
its bearer token through this module. The private key is never placed in a
clone environment, URL, job payload, database row, or log.

The private-key file is preferred over an environment variable because it can
be mounted read-only and re-read on refresh. The environment form remains
supported for secret managers that inject values directly; escaped newlines
are restored before parsing the PEM.

## Consequences

An internal deployment can act entirely as its GitHub App without a PAT and
without an operator rotating one-hour tokens. Token minting adds one GitHub API
request per process approximately hourly and a short synchronous wait on the
first credential resolution.

The App private key becomes the durable SCM secret. It grants the ability to
mint tokens for every installation of that App, so it belongs in a deployment
secret manager or sign-only key vault, never in the repository.

The deployment still has one configured installation ID. Running one Diffuse
instance across multiple organizations, selecting an installation from each
webhook, revocation cleanup, and encrypted per-installation storage remain
future work and must not be described as complete multi-tenancy.
