# ADR 0044: Hosted integration relay for self-hosted Diffuse nodes

Date: 2026-07-29

Status: Accepted

Supersedes the no-hosted-control-plane portion of
[ADR 0042](0042-source-available-self-hosted-distribution.md). The
source-available license and self-hosted review-engine distribution remain.

## Context

GitHub Apps and Slack Apps each have one public callback surface shared by all
installations. Requiring every Diffuse operator to create provider apps, expose
inbound ports, and keep webhook and OAuth callbacks available makes installation
harder and makes a residential outage lose provider events.

The review engine has a different trust boundary. Repository mirrors, indexes,
policies, model credentials, review history, and source-derived data should
remain on infrastructure chosen by the operator. Hosting the integration does
not require hosting that data or the compute that reviews it.

## Decision

Diffuse has two cooperating deployment roles:

1. **Diffuse Integration Relay**, operated by Diffuse, owns the shared GitHub
   and Slack App identities. It serves provider webhooks, OAuth and installation
   callbacks, verifies provider signatures, durably buffers delivery envelopes,
   maps installations to paired nodes, and brokers provider credentials.
2. **Diffuse Node**, operated by the customer, maintains an outbound-only
   authenticated connection to the relay. It durably ingests routed events,
   clones repositories directly from the provider, indexes and reviews locally,
   calls configured models, stores source-derived data, and publishes results
   directly to the provider.

The MVP pairs one node with one GitHub App installation. The relay routes by the
signed webhook payload's `installation.id`. A one-time high-entropy pairing code
is exchanged for a node credential; only SHA-256 digests of both are stored.
Re-pairing an installation rotates its node credential.

The relay acknowledges GitHub only after persisting the raw review delivery.
Installation lifecycle events are instead materialized directly as routing
state and are not queued to a node. A node leases routed deliveries and
acknowledges them only after the existing local webhook workflow has durably
accepted or intentionally ignored the event. Acknowledged payload bytes are
erased while delivery identity and digest remain.

The GitHub App private key never leaves the relay. A paired node may request a
one-hour installation token for its bound installation. The node uses it
directly with GitHub so repository contents do not transit the relay.

Slack will use the same pairing, leasing, acknowledgement, and retention model,
routed by `team_id`. Slack OAuth tokens remain relay-side unless a later ADR
establishes a short-lived delegation mechanism.

## Security boundary

- Provider webhook secrets, App private keys, and Slack client credentials live
  only in the hosted relay.
- Node credentials require HTTPS off loopback, are stored only as SHA-256 by the
  relay, and rotate on re-pairing.
- Nodes make outbound connections only. They expose no webhook ports.
- Delivery bodies may contain repository/workspace metadata and human-authored
  text, including quoted code. They are retained only while undelivered and
  removed on acknowledgement.
- Repository clones and file contents fetched by Diffuse, mirrors, embeddings,
  model prompts, model credentials, and durable review history are not relay
  data.
- A compromised relay can impersonate the shared Apps and observe incoming
  provider payloads. The App key should move to a sign-only KMS/HSM before
  public availability.

## Consequences

The public integration stays available while a node is offline, and queued work
resumes when it reconnects. Reviews cannot run while the node is unavailable;
the relay buffers rather than performs them.

The hosted deployment needs only a stateless HTTP service and durable
PostgreSQL. It runs no review worker and mounts no repository volume.

Multi-installation nodes, node failover, Slack delivery, retention jobs,
repository-narrowed provider tokens, formal SLOs, and fleet observability remain
follow-up work.
