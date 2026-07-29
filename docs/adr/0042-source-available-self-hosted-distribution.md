# ADR 0042: Source-available self-hosted distribution under BSL 1.1

Date: 2026-07-28

Status: Accepted

Supersedes ADR 0040.

## Context

ADR 0040 committed Diffuse to two things that no longer hold: proprietary
distribution gated behind a signed commercial agreement, and a managed cloud
service operated by Diffuse.

Both were expensive before they were useful. The proprietary model requires an
apparatus that does not exist and would have to be built before the first
customer: authenticated registry access provisioning, connected and offline
entitlement files, license expiry that fails without corrupting data, support
windows, and a sales motion to gate all of it. `deploy/README.md` still tells a
reader to "obtain read access to the private `ghcr.io/intuitumxyz/diffuse`
package and authenticate Docker using the customer credential supplied by
Diffuse" — a credential no process issues. The managed service is a larger
commitment still: tenant isolation, provisioning, metering, billing
reconciliation, regional placement, and fleet operations, all for a product
whose stated value is that source code never leaves the operator's environment.

The commercial-agreement gate also works against the product. Diffuse is
useful in proportion to how easily an operator can point it at their own
repositories and their own model credentials. Every step between reading about
it and running it against a real pull request costs adoption, and a
countersigned agreement is a large step.

Withholding the source has bought little in practice. ADR 0040 conceded the
point itself: because a self-hosted operator controls the host, no executable
distribution can prevent implementation recovery, and the packaged runtime
"raises the cost of inspection" without preventing it.

What does need protecting is narrower: nobody should be able to take Diffuse
and sell it back as a hosted review service. That is one restriction, not a
distribution model.

## Alternatives considered

- **Keep ADR 0040 (proprietary, commercial agreement, managed cloud).** Lost on
  cost and sequencing. It requires entitlement infrastructure, artifact
  delivery, and a sales process before the product has users, and it commits
  to operating a multi-tenant service that duplicates what self-hosted
  operators already run for themselves.

- **Apache-2.0 or MIT.** Maximum adoption and the simplest possible story, but
  it grants precisely the one thing worth withholding: any competitor could
  offer a hosted Diffuse. Rejected because it gives away the only commercial
  position for nothing in return.

- **AGPL-3.0.** Genuine open source, and its network copyleft means a hosted
  competitor must publish modifications. Rejected for two reasons: it still
  permits hosted competition on those terms, and blanket AGPL bans are common
  in exactly the risk-averse enterprises that want a self-hosted reviewer.
  The license would deter the target operator more than the target competitor.

- **Elastic License 2.0 / PolyForm Shield.** Comparable hosting restriction and
  no eventual conversion, which fits "not open source" more literally than BSL
  does. Rejected as less familiar; BSL's parameters are widely understood and
  the eventual conversion is an acceptable cost for that legibility. Revisit if
  the conversion becomes a real concern rather than a theoretical one.

## Decision

### BSL 1.1, proprietary and source-available, not open source

`LICENSE` is the Business Source License 1.1 with these parameters:

| Parameter | Value |
| --- | --- |
| Licensor | intuitumxyz |
| Change Date | 2030-07-28 |
| Change License | Apache License, Version 2.0 |
| Additional Use Grant | Production use, including commercial use, except offering Diffuse to third parties as a hosted, managed, or embedded service, or otherwise selling access to its functionality |

Diffuse is **proprietary, source-available software, not open source**. BSL 1.1
is not an OSI-approved license, and no document in this repository may describe
Diffuse as open source.

The current Licensed Work converts to Apache-2.0 on 2030-07-28, or the fourth
anniversary of its first public distribution, whichever comes first. The
license applies separately to each version; a later version may carry a
different Change Date, and conversion of an earlier version does not convert a
later one.

### What operators may and may not do

- **May**: read the source, modify it, run it in production, run it inside a
  commercial organization, process proprietary source code with it, and
  self-host it on any infrastructure they control.
- **May not**: offer Diffuse to third parties as a hosted, managed, or embedded
  service, or otherwise sell access to Diffuse's functionality, before the
  applicable Change Date.

Permitted self-hosted use requires no separate commercial agreement,
entitlement file, license key, or registry credential. Diffuse ships no
license-enforcement code; the restriction is contractual.

### Operators bring their own credentials

Model access is configured by the operator against their own provider accounts.
Diffuse does not broker, proxy, or resell model capacity, and it holds no
credential belonging to anyone but the operator running it.

### No managed cloud

Diffuse is self-hosted only. The cloud control plane, tenant isolation,
provisioning, entitlement, metering, and fleet-operation work described in ADR
0040 is withdrawn rather than deferred. Phase 7 is removed from the roadmap and
the managed-cloud capability row is removed from the ledger.

PostgreSQL plus pgvector remains authoritative for all repository, snapshot,
source-derived, retrieval, review, finding, workflow, feedback, learning,
publication, and authorization state — unchanged from ADR 0040, and no longer
qualified by a second deployment mode.

## Consequences

Distribution becomes ordinary. Release automation publishes public images; the
self-host bundle stops being an entitlement-gated artifact and becomes a
Compose file anyone can pull. Signing, provenance, and SBOMs are retained —
they are supply-chain hygiene, not access control. `.github/workflows/release.yml`
verifies that both the image and bundle are anonymously pullable before it
creates a GitHub Release. Repository and package visibility are GitHub settings,
not workflow inputs; administrators must make them public after this decision
lands on `main`. A newly created bundle package may require one failed publish,
the visibility change, and a release re-run because GitHub creates packages
private.

The confidentiality and no-derivative-works restrictions disappear, so
operators may fork and patch. Contributions become possible, and with them the
need for a contribution policy this repository does not yet have.

Diffuse gives up managed-service revenue and the option to run the product for
customers who do not want to operate it. That option is recoverable — the
Licensor is not bound by its own Additional Use Grant — but the engineering
committed to it is not, and DEV-242 should be closed rather than left in the
backlog.

BSL costs some adoption. Policies that permit only OSI-approved licenses will
treat Diffuse as proprietary, and the "not open source" qualifier has to be
stated every time distribution is described, because readers will assume
otherwise once the repository is public.

The 2030-07-28 Change Date is a real commitment. The current Licensed Work
becomes Apache-2.0 no later than that date whether or not the hosting
restriction still matters commercially.
