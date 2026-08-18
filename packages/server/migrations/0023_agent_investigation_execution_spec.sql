-- Bind each durable native investigation to the immutable execution spec the
-- worker dispatched, including the host's terminal error code when one fails.
ALTER TABLE agent_investigations
    ADD COLUMN execution_spec_version SMALLINT NOT NULL DEFAULT 0
        CHECK (execution_spec_version IN (0, 1)),
    ADD COLUMN base_sha CHAR(40)
        CHECK (base_sha IS NULL OR base_sha ~ '^[0-9a-f]{40}$'),
    ADD COLUMN context_plan_fingerprint CHAR(64)
        CHECK (
            context_plan_fingerprint IS NULL
            OR context_plan_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN role TEXT
        CHECK (role IS NULL OR role IN ('candidate', 'verifier')),
    ADD COLUMN turn_budget INTEGER
        CHECK (turn_budget IS NULL OR turn_budget > 0),
    ADD COLUMN timeout_seconds INTEGER
        CHECK (timeout_seconds IS NULL OR timeout_seconds > 0),
    ADD COLUMN max_result_bytes INTEGER
        CHECK (max_result_bytes IS NULL OR max_result_bytes > 0),
    ADD COLUMN source_archive_digest CHAR(64)
        CHECK (
            source_archive_digest IS NULL
            OR source_archive_digest ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN source_manifest_digest CHAR(64)
        CHECK (
            source_manifest_digest IS NULL
            OR source_manifest_digest ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN input_result_digest CHAR(64)
        CHECK (
            input_result_digest IS NULL
            OR input_result_digest ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN error_code TEXT
        CHECK (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,99}$');

UPDATE agent_investigations AS session
SET base_sha = review.base_sha
FROM review_runs AS review
WHERE review.workflow_job_id = session.review_job_id
  AND review.repository_id = session.repository_id
  AND review.pull_request_id = session.pull_request_id
  AND review.head_sha = session.head_sha
  AND session.base_sha IS NULL;

UPDATE agent_investigations
SET role = 'candidate',
    turn_budget = 24,
    timeout_seconds = 600,
    max_result_bytes = 256000
WHERE role IS NULL
   OR turn_budget IS NULL
   OR timeout_seconds IS NULL
   OR max_result_bytes IS NULL;

UPDATE agent_investigations
SET error_code = CASE status
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'failed' THEN 'runner_execution_failed'
    ELSE error_code
END
WHERE error_code IS NULL
  AND status IN ('failed', 'cancelled');

-- Historical rows cannot recover source-archive or context-plan digests that
-- were never stored. Version 0 names that limitation explicitly; every new
-- dispatch is version 1 and must carry the complete immutable specification.
ALTER TABLE agent_investigations
    ADD CONSTRAINT agent_investigations_complete_execution_spec_check
    CHECK (
        execution_spec_version = 0
        OR (
            base_sha IS NOT NULL
            AND context_plan_fingerprint IS NOT NULL
            AND role IS NOT NULL
            AND turn_budget IS NOT NULL
            AND timeout_seconds IS NOT NULL
            AND max_result_bytes IS NOT NULL
            AND source_archive_digest IS NOT NULL
            AND source_manifest_digest IS NOT NULL
            AND (
                (role = 'candidate' AND input_result_digest IS NULL)
                OR (role = 'verifier' AND input_result_digest IS NOT NULL)
            )
        )
    );

-- Existing rows retain version 0 from the ADD COLUMN backfill. Every row
-- created after this migration defaults to the complete version-1 contract.
ALTER TABLE agent_investigations
    ALTER COLUMN execution_spec_version SET DEFAULT 1;
