-- Align the durable Agent investigation table with the runtime store name.
-- Migration 0016 created agent_sessions; Python persists agent_investigations.
ALTER TABLE agent_sessions RENAME TO agent_investigations;
ALTER INDEX agent_sessions_expiry_idx RENAME TO agent_investigations_expiry_idx;
