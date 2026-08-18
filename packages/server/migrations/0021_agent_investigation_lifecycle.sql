-- A runner is not a synchronous function call: preserve placement and its
-- observable execution state so a worker can reconnect to the same immutable
-- investigation after a transport interruption without dispatching another.
ALTER TABLE agent_investigations
    ADD COLUMN runner_id TEXT,
    ADD COLUMN accepted_at TIMESTAMPTZ,
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN cancel_requested_at TIMESTAMPTZ;

ALTER TABLE agent_investigations
    DROP CONSTRAINT agent_sessions_status_check;

ALTER TABLE agent_investigations
    ADD CONSTRAINT agent_investigations_status_check
    CHECK (status IN ('dispatched', 'accepted', 'running', 'completed', 'failed', 'cancelled', 'expired'));

CREATE INDEX agent_investigations_active_runner_idx
    ON agent_investigations (runner_id, last_heartbeat_at)
    WHERE status IN ('accepted', 'running');
