# ADR 0040: Proprietary self-hosted and managed-cloud distribution

Date: 2026-07-25

Status: Superseded by [ADR 0042](0042-source-available-self-hosted-distribution.md).
Diffuse is proprietary, source-available software under BSL 1.1 and self-hosted
only; the commercial-agreement distribution and the managed cloud service
described below are withdrawn. The PostgreSQL-authoritative decision survives
in ADR 0042.

## Context

Diffuse must support customers that require their repository source, mirrors,
embeddings, findings, and model traffic to remain inside infrastructure they
control. It must also support customers that prefer to buy a managed service
operated by Diffuse.

Self-hostable does not imply open source. Diffuse's implementation repository
remains private and the software remains proprietary. Self-hosted customers
receive executable artifacts and installation rights under a commercial
agreement, not access to the source repository.

Because a self-hosted operator controls the host, no executable distribution
can make implementation recovery impossible. The previous Python image was
especially inspectable because it copied the source checkout into the runtime
image. The packaged runtime now removes that checkout and plain Python source;
compilation, minimal images, and signed artifacts raise the cost of inspection,
but contractual restrictions remain part of the protection model.

PostgreSQL and pgvector are already part of Diffuse's correctness and retrieval
model. They hold immutable index snapshots, graph and vector evidence, review
state, durable workflow leases, idempotency records, feedback, learning, and
publication state. Replacing this data plane with a hosted application database
would weaken standalone operation and create a second implementation of those
semantics.

## Decision

### One product, two primary deployment modes

Diffuse will ship the same versioned application and migration artifacts in
both modes:

1. **Proprietary self-hosted.** The customer operates the Diffuse API, workers,
   PostgreSQL/pgvector, repository storage, model connections, and optional web
   application. Standalone operation has no required Diffuse-hosted control
   plane. Supported connected and air-gapped installation profiles may use
   different update and entitlement mechanisms, but both execute entirely in
   customer-controlled infrastructure.
2. **Managed cloud.** Diffuse operates the same data-plane services and
   PostgreSQL contract on the customer's behalf. A cloud control plane adds
   account, subscription, entitlement, provisioning, regional placement,
   deployment registry, support, and fleet-operation capabilities.

The managed service is a deployment and operations offering, not a divergent
review engine or alternative database implementation.

### PostgreSQL remains authoritative

- PostgreSQL plus pgvector remains the canonical store for all repository,
  snapshot, source-derived, retrieval, review, finding, workflow, feedback,
  learning, publication, and local authorization state.
- The schema and checksum-verified migration catalog ship with every executable
  release. Self-hosted customers may use the supported bundled PostgreSQL
  profile or an external compatible PostgreSQL service.
- The open REST, webhook, CLI, and MCP contracts form the stable boundary
  around the data plane. Diffuse will not build a lowest-common-denominator
  storage abstraction that attempts to run the engine on both PostgreSQL and
  an application database.

### A future cloud control plane is separate and optional

- A cloud control plane may use Convex or another application backend, but it
  is not a runtime dependency of standalone Diffuse.
- It may own cloud-only customer accounts, billing references, entitlements,
  deployment registry, provisioning state, notification preferences, and
  rebuildable fleet/status projections.
- It does not become authoritative for repository snapshots, source chunks,
  diffs, embeddings, findings, review evidence, workflow state, or learned
  rules.
- Connected data planes exchange signed, versioned, idempotent commands and
  events. Each fact has one authoritative owner; projections must be disposable
  and rebuildable.
- Self-hosted telemetry and support diagnostics are opt-in, bounded, and
  documented. Source-derived content is excluded from the cloud control-plane
  contract.

### Proprietary artifact distribution

- Release automation builds immutable, versioned, multi-architecture OCI
  images from the private repository and publishes them to an authenticated
  registry.
- Customer installation manifests reference image digests rather than mutable
  tags. Images, manifests, update bundles, and software bills of materials are
  signed and accompanied by verifiable provenance.
- The production runtime image excludes the source checkout, Git metadata,
  tests, build tools, and unrelated documentation. The Python application is
  compiled or otherwise packaged to avoid distributing plain source files,
  with no claim that this prevents a determined operator from reverse
  engineering the executable.
- Connected installations may use online subscription and update entitlement.
  Air-gapped installations use time-bounded or perpetual signed offline
  entitlement files and separately delivered signed update bundles.
- License enforcement must fail predictably without corrupting customer data.
  Expiration may prevent upgrades or new paid work according to the commercial
  policy, but it must not block backup, export, or safe shutdown.

## Consequences

Customers can choose operational control without receiving the private Git
repository, while cloud customers buy the same product as a managed service.
The review engine, migrations, compatibility contract, and evidence model do
not fork between deployment modes.

Diffuse must operate a secure software-supply-chain and customer artifact
delivery system. It must define upgrade support windows, entitlement behavior,
backup/export guarantees, and incident response for both connected and
air-gapped customers. Native compilation and minimal images increase build and
debugging complexity without providing perfect secrecy.

A future Convex control plane can accelerate the managed product without
placing customer source-derived data there or weakening standalone operation.
If the control-plane experiment is abandoned, the PostgreSQL-backed product
and its self-hosted distribution remain unaffected.
