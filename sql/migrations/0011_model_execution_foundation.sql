ALTER TABLE review_runs
    ADD COLUMN executor TEXT NOT NULL DEFAULT 'litellm',
    ADD COLUMN execution_plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN execution_plan_fingerprint TEXT NOT NULL
        DEFAULT '0000000000000000000000000000000000000000000000000000000000000000';

ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_executor
        CHECK (executor IN ('litellm', 'codex-cli', 'claude-cli')),
    ADD CONSTRAINT review_runs_execution_plan_object
        CHECK (
            jsonb_typeof(execution_plan) = 'object'
            AND octet_length(execution_plan::text) <= 65536
        ),
    ADD CONSTRAINT review_runs_execution_plan_fingerprint
        CHECK (execution_plan_fingerprint ~ '^[0-9a-f]{64}$');

CREATE TABLE review_generation_steps (
    review_run_id          BIGINT NOT NULL
                               REFERENCES review_runs (id) ON DELETE CASCADE,
    step_key               TEXT NOT NULL
                               CHECK (step_key ~ '^[a-z0-9][a-z0-9_/-]{0,127}$'),
    request_fingerprint    TEXT NOT NULL
                               CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    response_schema        TEXT NOT NULL CHECK (
                               length(response_schema) BETWEEN 1 AND 512
                           ),
    response               JSONB NOT NULL CHECK (
                               jsonb_typeof(response) = 'object'
                               AND octet_length(response::text) <= 1048576
                           ),
    prompt_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    resolved_model         TEXT CHECK (
                               resolved_model IS NULL
                               OR length(resolved_model) BETWEEN 1 AND 512
                           ),
    executor_version       TEXT CHECK (
                               executor_version IS NULL
                               OR length(executor_version) BETWEEN 1 AND 255
                           ),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (review_run_id, step_key)
);
