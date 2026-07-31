-- Retrieval is graph + lexical only. The vector leg is gone, so the columns and
-- the extension that served it are dead weight: every row carried a 1,536-float
-- vector nothing reads, and the schema required an extension a plain PostgreSQL
-- image does not ship.
--
-- Snapshot compatibility now rests on `index_format_version` alone. That version
-- is bumped in the same change, so snapshots built by an older Diffuse are
-- already incompatible and will be rebuilt; no row here needs rewriting.
--
-- Note for the image pin: `sql/schema.sql` is the frozen version-1 migration and
-- still runs `CREATE EXTENSION IF NOT EXISTS vector`, so a *fresh* install still
-- needs an image that ships pgvector even though nothing uses it after this
-- migration. Swapping the Postgres image for stock `postgres:17` requires
-- re-cutting the version-1 baseline, which ADR 0036 deliberately forbids.

DROP INDEX IF EXISTS code_chunks_embedding_hnsw_idx;

ALTER TABLE code_chunks
    DROP COLUMN IF EXISTS embedding;

-- Dropping the columns would drop this index implicitly; naming it keeps the
-- rebuild below adjacent to the removal it compensates for.
DROP INDEX IF EXISTS index_snapshots_one_build_per_revision_idx;

ALTER TABLE index_snapshots
    DROP COLUMN IF EXISTS embedding_model,
    DROP COLUMN IF EXISTS embedding_dimensions;

CREATE UNIQUE INDEX IF NOT EXISTS index_snapshots_one_build_per_revision_idx
    ON index_snapshots (
        repository_id,
        commit_sha,
        index_format_version
    )
    WHERE status = 'building';

-- RESTRICT (the default) on purpose: if an operator built something else on
-- pgvector in Diffuse's database, this fails the whole upgrade transaction
-- rather than cascading through objects Diffuse does not own.
DROP EXTENSION IF EXISTS vector;
