# ADR 0043: Graph and lexical retrieval without embeddings

Date: 2026-07-31

Status: Accepted

## Context

Embedding is not a step inside indexing that a CLI rewrite could relocate. It
also runs at query time: every retrieval called `embed_text(query)` before it
could touch PostgreSQL. So an embedding model, a credential for it, and a
network round trip were on the hot path of every review and every code query,
not just of `diffuse index`.

That single dependency was the source of most of Diffuse's configuration
surface and several standing defects:

- Anthropic — the recommended and default review provider — has no embeddings
  API, so an operator who configured Diffuse correctly for review still could
  not index without a second provider's key. `indexer/embed.py` refused to start
  without one.
- `EMBEDDING_DIMENSIONS` was configurable in name only: the version-1 baseline
  declares `VECTOR(1536)` with a `CHECK` pinning the width, so any model
  returning a different width failed every index job at insert time.
- The embedding path had no equivalent of `REVIEW_API_BASE` and resolved a
  credential only for OpenAI model names, so a self-hosted embedding endpoint
  was unreachable without redirecting the review model with it.
- `pgvector` pinned the Postgres image to a third-party build.

What the vector leg bought was one of three retrieval channels, weighted 1.0
against graph's 3.0 and lexical's 1.8 (ADR 0006).

## Alternatives considered

- **Ship `EMBEDDING_API_BASE` (PR #46)** — a per-call base URL so an operator
  could point embeddings at Ollama, vLLM, or a LiteLLM proxy without
  redirecting the review model. It works, and it was written. It lost because
  it makes the credential problem configurable rather than absent: the operator
  still has to choose an embedding model, run a second endpoint, and keep its
  output width at exactly 1536. It also left `EMBEDDING_DIMENSIONS`, the
  `VECTOR(1536)` pin, and the pgvector image untouched.
- **Migrate to an unsized `vector` column so dimensions become real** —
  rejected on a measured platform constraint: pgvector cannot build an HNSW or
  IVFFlat index on an unsized `vector` column in any released version
  (`ERROR: column does not have dimensions`). The only index-preserving shape
  is a partial expression index per width, which turns one knob into a
  per-installation schema.
- **Keep the vector leg and accept the configuration cost** — the honest
  default. It lost to the argument below, but see the cost section: this is the
  decision to revisit first if review quality drops.

## Decision

Retrieval is graph and lexical only. Diffuse computes no embeddings and stores
no vectors.

- `indexer/embed.py` is deleted, along with `EMBEDDING_MODEL`,
  `EMBEDDING_DIMENSIONS`, `EMBEDDING_BATCH_SIZE`, `MIN_CONTEXT_SIMILARITY`, and
  the `OPENAI_API_KEY` requirement for indexing. `verify_embedding_credential`
  and the worker's `validate_worker_credentials` startup check go with them.
- `retriever/retrieve.py` fuses two channels. Weighted reciprocal rank fusion
  keeps graph at 3.0 and lexical at 1.8; the semantic channel and its
  similarity floor are gone, and `RetrievedContext` no longer carries a
  `similarity`. The MCP code-search payload drops `retrieval.similarity` with
  it.
- A free-form code question has no changed lines to seed the graph walk from,
  so `retrieve_query_context_from_plan` seeds it from that snapshot's top
  lexical hits alone.
- Snapshot compatibility is `index_format_version` alone. `begin_index_snapshot`,
  `active_snapshot_id`, `search_lexical`, `search_graph_related_chunks`, and
  `resolve_cross_repository_context` no longer take a model or a dimension.
  `INDEX_FORMAT_VERSION` becomes `diffuse-index-v4-graph-lexical-*`, so every
  existing snapshot is rebuilt rather than reused under provenance the schema
  no longer records.
- Migration `0010_drop_embeddings` drops `code_chunks.embedding`, its HNSW
  index, `index_snapshots.embedding_model`, `index_snapshots.embedding_dimensions`,
  and the `vector` extension, and rebuilds the build-uniqueness index on
  `(repository_id, commit_sha, index_format_version)`.
- `sql/schema.sql` stays frozen (ADR 0036). `_verify_baseline_contract`
  therefore exempts the three baseline columns 0010 removes, via
  `RETIRED_BASELINE_COLUMNS`, and no longer requires the `vector` extension.

## Consequences

Indexing needs no model credential at all. A single review-model key is the
whole model configuration, `diffuse init` has three fewer questions to guess at,
and Anthropic-only installations work. The database is plain PostgreSQL.

**This is a judgment call with no instrument, and the instrument is not
coming.** `service/eval_harness.py` reads context verbatim from fixture files —
`FixtureContext` becomes `RetrievedContext` — so the harness measures the review
engine, not retrieval. No golden, present or future, would detect a
retrieval-quality regression from this change. It was accepted on the reasoning
above; git makes it reversible. **If review quality visibly drops after this
lands, suspect this first.**

Conceptual queries that name nothing in the codebase — "how does rate limiting
work?" against a repository that spells it `TokenBucket` — now depend entirely
on lexical overlap and the graph edges around whatever that overlap finds.
Diff-grounded review is the least exposed case: the changed lines are known, and
what a review needs is their callers and contracts, which is the graph channel's
job.

One dependency survives the strip. The frozen version-1 baseline still runs
`CREATE EXTENSION IF NOT EXISTS vector`, which stock `postgres:17` cannot
satisfy, so a fresh install still needs an image that ships pgvector even though
0010 drops the extension immediately afterwards. Removing that last tie means
re-cutting version 1, which ADR 0036 deliberately forbids; the image pins in
`docker-compose.yml`, `deploy/compose.yaml`, and `.github/workflows/verify.yml`
carry a comment saying so.

This supersedes the vector leg of ADR 0006 and the embedding-model/dimension
component of the snapshot identity in ADR 0001. Both remain accurate on
everything else. Operators upgrading must apply migrations before swapping the
Postgres image, and every repository is re-indexed on the format bump.
