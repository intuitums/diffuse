-- Index the foreign keys and predicates on the review and queue hot paths.
--
-- PostgreSQL does not index foreign keys automatically. The lineage columns
-- below are joined on every review persist and every review-comment delivery,
-- and the queue reclaim predicate runs before every job claim, so each was a
-- sequential scan that degraded with table growth.
--
-- These are plain (non-CONCURRENT) index builds because Diffuse applies
-- migrations inside a transaction; they take a SHARE lock that blocks writes
-- to each table for the duration of the build.

CREATE INDEX IF NOT EXISTS review_findings_lineage_idx
    ON review_findings (lineage_id, id DESC);

CREATE INDEX IF NOT EXISTS finding_lineage_events_finding_idx
    ON finding_lineage_events (finding_id)
    WHERE finding_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS review_runs_repository_started_idx
    ON review_runs (repository_id, started_at);

CREATE INDEX IF NOT EXISTS workflow_jobs_repository_idx
    ON workflow_jobs (repository_id);

CREATE INDEX IF NOT EXISTS workflow_jobs_lease_reclaim_idx
    ON workflow_jobs (lease_expires_at)
    WHERE status = 'running';
