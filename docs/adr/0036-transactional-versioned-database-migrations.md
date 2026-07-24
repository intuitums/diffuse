# ADR 0036: Transactional versioned database migrations

Date: 2026-07-24

Status: Accepted

## Context

Diffuse's review, index, policy, feedback, analytics, and workflow state is
durable PostgreSQL data. A fresh-install-only schema cannot support a
self-hosted product: applying a new image against an older volume could start
workers with missing columns, while silently rerunning `CREATE TABLE IF NOT
EXISTS` does not upgrade existing tables.

Multiple API or worker replicas may be deployed at once, historical migration
files can be changed accidentally, and an installation created before the
migration ledger may already contain valuable data. Upgrade behavior therefore
needs serialization, immutable identity, atomic failure, and an explicit
legacy boundary.

Relevant platform contracts:

- <https://www.postgresql.org/docs/current/functions-admin.html>
- <https://www.postgresql.org/docs/current/ddl.html>
- <https://docs.docker.com/compose/how-tos/startup-order/>

## Decision

- Freeze `sql/schema.sql` as migration version 1 with a source-controlled
  SHA-256 digest. Discover only consecutive `NNNN_short_name.sql` files for
  later versions. Package the SQL with the Python distribution.
- Acquire one transaction-scoped PostgreSQL advisory lock before inspecting or
  changing migration state. Apply every pending version and its ledger row in
  the same transaction. A concurrent migrator waits, then observes a no-op.
- Record version, name, checksum, explicit-adoption state, actor, execution
  duration, and application timestamp in `diffuse_schema_migrations`.
- Require the applied versions to be a consecutive prefix of the local
  catalog, and require every recorded name and checksum to match. Refuse
  startup when the database is ahead of the binary or history has drifted.
- Treat a database with application tables and no ledger as unversioned.
  Refuse automatic adoption. `--adopt-existing` is an explicit operator
  attestation and succeeds only after every baseline table and column plus the
  pgvector extension is verified.
- Expose `diffuse database migrate`, `status`, and `verify`. In the supported
  Compose profile, a one-shot migration service waits for database health and
  API/worker services wait for that migration to complete successfully.

## Consequences

Fresh installation, repeat startup, and simultaneous startup converge on one
schema version without duplicate DDL. An edited historical file, corrupted
ledger, incomplete legacy schema, or newer-than-binary database fails closed
before application services start.

Version 1 is intentionally immutable, so future embedding dimensions, tables,
columns, indexes, and constraints require a new migration even when changing a
fresh-install snapshot would appear simpler. Rollback automation is not
implied: releases must still define backup, restore, downgrade compatibility,
and destructive-migration policy before production support is claimed.
