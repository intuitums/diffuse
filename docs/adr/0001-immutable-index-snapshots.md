# ADR 0001: Immutable commit-pinned index snapshots

- Status: accepted
- Date: 2026-07-23

## Context

A mutable set of chunks and graph rows keyed only by `owner/repository` cannot
prove which source revision informed a review. Concurrent indexers can mix
commits, model changes can silently reuse incompatible embeddings, and deleting
stale rows destroys evidence needed to reproduce old reviews.

## Decision

Each repository index is an immutable snapshot identified by repository, Git
commit, index-format version, embedding model, and embedding dimensions.

- A snapshot moves through `building`, `active`, `failed`, or `superseded`.
- Only one snapshot per repository is active.
- Chunks, symbols, and relationships belong to exactly one snapshot.
- A new snapshot becomes active only after expected row counts validate inside
  the same transaction.
- Activation supersedes the previous active snapshot without deleting it.
- A build cannot activate if a newer viable build request exists.
- Repository-scoped advisory locks serialize snapshot creation.
- Recent duplicate builds are coalesced; stale abandoned builds become failed
  and may be retried.
- Unchanged chunks copy their stored embeddings from a compatible prior
  snapshot instead of calling the embedding provider again.
- Retrieval resolves the active snapshot whose model and dimensions match the
  current embedding configuration and whose format matches the running
  indexer/retriever.

Native review runs store the exact snapshot IDs they use.

## Consequences

Historical indexes consume more storage, so a future retention job must delete
snapshots only when review/evidence policy permits. In exchange, indexing is
atomic, reviews can become reproducible, embedding migrations are explicit,
and concurrent build ordering is deterministic.

The current schema fixes vector width at 1,536 dimensions. Operators using a
different width must migrate the vector column and snapshot constraint
together; future migrations will automate this.
