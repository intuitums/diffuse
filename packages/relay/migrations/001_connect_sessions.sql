-- Additive upgrade for existing Integration Service databases.
-- Safe to re-run. Renames legacy setup_* / event_signing_key names to connect_* /
-- delivery_signing_key, then adds connect-session support.
--
-- Apply in Neon SQL Editor as one statement.
-- Fresh empty databases should use ../schema.sql instead (via migrate.py).

DO $migrate$
BEGIN
    -- Vocabulary: setup → connect (tables that already exist in production).
    IF to_regclass('public.setup_oauth_states') IS NOT NULL
       AND to_regclass('public.connect_oauth_states') IS NULL THEN
        ALTER TABLE setup_oauth_states RENAME TO connect_oauth_states;
    END IF;

    IF to_regclass('public.setup_enrollment_codes') IS NOT NULL
       AND to_regclass('public.connect_enrollment_codes') IS NULL THEN
        ALTER TABLE setup_enrollment_codes RENAME TO connect_enrollment_codes;
    END IF;

    -- Column vocabulary: installation_id → github_installation_id on oauth states.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'connect_oauth_states'
          AND column_name = 'installation_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'connect_oauth_states'
          AND column_name = 'github_installation_id'
    ) THEN
        ALTER TABLE connect_oauth_states
            RENAME COLUMN installation_id TO github_installation_id;
    END IF;

    -- Column vocabulary: event_signing_key → delivery_signing_key.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'self_hosted_instances'
          AND column_name = 'event_signing_key'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'self_hosted_instances'
          AND column_name = 'delivery_signing_key'
    ) THEN
        ALTER TABLE self_hosted_instances
            RENAME COLUMN event_signing_key TO delivery_signing_key;
    END IF;

    CREATE TABLE IF NOT EXISTS connect_sessions (
        id UUID PRIMARY KEY,
        poll_secret_hash TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'consumed', 'failed')),
        github_installation_id BIGINT REFERENCES app_installations (github_installation_id)
            ON DELETE SET NULL,
        instance_id UUID,
        instance_token_sealed TEXT,
        delivery_signing_key_sealed TEXT,
        candidate_installations JSONB,
        authorized_github_user_id BIGINT,
        authorized_github_login TEXT,
        error_message TEXT,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- If an earlier draft created event_signing_key_sealed, rename it.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'connect_sessions'
          AND column_name = 'event_signing_key_sealed'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'connect_sessions'
          AND column_name = 'delivery_signing_key_sealed'
    ) THEN
        ALTER TABLE connect_sessions
            RENAME COLUMN event_signing_key_sealed TO delivery_signing_key_sealed;
    END IF;

    CREATE INDEX IF NOT EXISTS connect_sessions_pending_expires_idx
        ON connect_sessions (expires_at)
        WHERE status = 'pending';

    ALTER TABLE connect_oauth_states
        ADD COLUMN IF NOT EXISTS connect_session_id UUID;

    -- Session-only OAuth states omit github_installation_id.
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'connect_oauth_states'
          AND column_name = 'github_installation_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE connect_oauth_states
            ALTER COLUMN github_installation_id DROP NOT NULL;
    END IF;
END;
$migrate$;
