-- Additive upgrade for existing Integration Service databases.
-- Safe to re-run. Does not recreate or rewrite existing tables/data.
--
-- Apply in Neon SQL Editor (or Vercel → Storage → Neon → Query) as one
-- statement. New empty databases should use ../vercel_schema.sql instead.

DO $migrate$
BEGIN
    CREATE TABLE IF NOT EXISTS connect_sessions (
        id UUID PRIMARY KEY,
        poll_secret_hash TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'consumed', 'failed')),
        github_installation_id BIGINT REFERENCES app_installations (github_installation_id)
            ON DELETE SET NULL,
        instance_id UUID,
        instance_token_sealed TEXT,
        event_signing_key_sealed TEXT,
        candidate_installations JSONB,
        authorized_github_user_id BIGINT,
        authorized_github_login TEXT,
        error_message TEXT,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS connect_sessions_pending_expires_idx
        ON connect_sessions (expires_at)
        WHERE status = 'pending';

    ALTER TABLE setup_oauth_states
        ADD COLUMN IF NOT EXISTS connect_session_id UUID;

    -- Session-only OAuth states omit installation_id. Only drop NOT NULL when
    -- the live column is still required; do not swallow unrelated errors.
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'setup_oauth_states'
          AND column_name = 'installation_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE setup_oauth_states
            ALTER COLUMN installation_id DROP NOT NULL;
    END IF;
END;
$migrate$;
