-- Durable control-plane record for one native agent dispatch. Credentials and
-- bearer capabilities never enter this table: only the capability identifier
-- and a one-way SHA-256 digest are retained for audit/replay binding.
CREATE TABLE agent_sessions (
    id UUID PRIMARY KEY,
    review_job_id BIGINT NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    runtime TEXT NOT NULL CHECK (runtime IN ('claude', 'codex')),
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE RESTRICT,
    snapshot_id BIGINT NOT NULL REFERENCES index_snapshots(id) ON DELETE RESTRICT,
    head_sha CHAR(40) NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    capability_id TEXT NOT NULL UNIQUE,
    capability_hash CHAR(64) NOT NULL CHECK (capability_hash ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('dispatched', 'completed', 'failed', 'cancelled', 'expired')),
    result_digest CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX agent_sessions_expiry_idx ON agent_sessions (expires_at)
    WHERE status = 'dispatched';
