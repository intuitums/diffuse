-- Preserve each safe indexed source file once per immutable snapshot so a
-- literal code query does not have to infer terms or scan overlapping chunks.
-- pg_trgm is part of PostgreSQL's supported contrib extensions and makes
-- `content LIKE '%' || literal || '%'` index-backed for literals of 3+ bytes.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE repository_files (
    id           BIGSERIAL PRIMARY KEY,
    snapshot_id  BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    file_path    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, file_path)
);

CREATE INDEX repository_files_snapshot_path_idx
    ON repository_files (snapshot_id, file_path);

CREATE INDEX repository_files_content_trgm_idx
    ON repository_files USING gin (content gin_trgm_ops);
