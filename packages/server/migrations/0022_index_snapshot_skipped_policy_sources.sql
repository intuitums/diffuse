-- Guidance sources policy discovery refused to read (e.g. a symlinked
-- CLAUDE.md), recorded per snapshot so operators can see that a repository is
-- reviewed without guidance it appears to have.
ALTER TABLE index_snapshots
    ADD COLUMN skipped_policy_sources JSONB NOT NULL DEFAULT '[]'::jsonb;
