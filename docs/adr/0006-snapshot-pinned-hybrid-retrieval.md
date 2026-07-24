# ADR 0006: Snapshot-pinned hybrid retrieval

- Status: accepted
- Date: 2026-07-23

## Context

Embeddings are good at conceptual similarity but often miss exact API names,
error codes, configuration keys, and uncommon identifiers. Graph traversal
finds known relationships but cannot help when an adapter has not resolved an
edge. Filling the remaining context budget with vector results alone therefore
does not provide Greptile-like whole-codebase grounding.

Raw diffs cannot be passed directly to PostgreSQL query syntax: punctuation,
operators, comments, and untrusted text could produce invalid or unexpectedly
expensive queries.

## Decision

Diffuse fuses three independent retrieval channels from one immutable index
snapshot.

- Every code chunk has a generated PostgreSQL `tsvector` using the `simple`
  configuration. File paths and symbol names have weight `A`; chunk content has
  weight `B`; a GIN index supports bounded lookup.
- The retriever extracts at most 24 ASCII code identifiers from added, removed,
  and path lines. Language keywords are removed, specific camel/snake-case and
  long identifiers receive a small priority bonus, and terms are validated
  again at the store boundary.
- Validated terms are encoded through `websearch_to_tsquery`; raw diff syntax
  never becomes a tsquery.
- Graph, lexical, and semantic channels each retrieve a bounded candidate set.
  Changed files are excluded from lexical and semantic reference results.
- Semantic candidates below the configured similarity floor are discarded.
- Weighted reciprocal rank fusion uses graph, lexical, and semantic weights of
  3.0, 1.8, and 1.0 respectively. A chunk appearing in multiple channels is
  promoted without depending on incomparable raw rank scales.
- Ordering is deterministic after score ties.
- Returned context includes combined channel reasons, the hybrid score,
  semantic similarity when applicable, exact path/line provenance, and the
  snapshot ID selected before retrieval begins.

## Consequences

Exact names can retrieve relevant code even when embeddings or graph linking
miss it, while graph neighbors retain priority and multi-channel agreement
raises confidence. The implementation remains self-contained in PostgreSQL and
pgvector for the small-installation profile.

The `simple` text-search configuration is language-agnostic and is not a
compiler-aware lexical index. The foundation does not yet implement BM25,
symbol-prefix/fuzzy search, query expansion from generated summaries,
multi-hop graph planning, cross-repository fusion, learned reranking, or
offline retrieval-quality evaluation. Those remain parity work.
