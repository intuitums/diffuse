-- One statement for Vercel's Marketplace Query editor, which uses prepared
-- statements and therefore cannot accept the multi-statement schema.sql file.
-- This has the same idempotent schema as schema.sql.
DO $schema$
BEGIN
    CREATE TABLE IF NOT EXISTS setup_oauth_states (
        state_hash TEXT PRIMARY KEY,
        installation_id BIGINT CHECK (installation_id IS NULL OR installation_id > 0),
        connect_session_id UUID,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (installation_id IS NOT NULL OR connect_session_id IS NOT NULL)
    );

    CREATE TABLE IF NOT EXISTS app_installations (
        github_installation_id BIGINT PRIMARY KEY CHECK (github_installation_id > 0),
        github_user_id BIGINT NOT NULL CHECK (github_user_id > 0),
        github_login TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS setup_enrollment_codes (
        code_hash TEXT PRIMARY KEY,
        github_installation_id BIGINT NOT NULL REFERENCES app_installations (github_installation_id)
            ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL,
        redeemed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

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

    -- event_signing_key is AES-GCM sealed under
    -- DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK (see sealed_secret.py).
    CREATE TABLE IF NOT EXISTS self_hosted_instances (
        id UUID PRIMARY KEY,
        github_installation_id BIGINT NOT NULL REFERENCES app_installations (github_installation_id)
            ON DELETE CASCADE,
        display_name TEXT NOT NULL,
        credential_hash TEXT NOT NULL UNIQUE,
        event_signing_key TEXT NOT NULL,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS self_hosted_instances_live_installation_idx
        ON self_hosted_instances (github_installation_id)
        WHERE revoked_at IS NULL;

    CREATE TABLE IF NOT EXISTS github_webhook_events (
        delivery_id TEXT PRIMARY KEY,
        github_installation_id BIGINT NOT NULL REFERENCES app_installations (github_installation_id)
            ON DELETE CASCADE,
        event_name TEXT NOT NULL,
        payload JSONB NOT NULL,
        payload_sha256 TEXT NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS github_webhook_events_installation_received_idx
        ON github_webhook_events (github_installation_id, received_at);

    CREATE TABLE IF NOT EXISTS webhook_event_deliveries (
        delivery_id TEXT NOT NULL REFERENCES github_webhook_events (delivery_id) ON DELETE CASCADE,
        instance_id UUID NOT NULL REFERENCES self_hosted_instances (id) ON DELETE CASCADE,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        leased_until TIMESTAMPTZ,
        acknowledged_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (delivery_id, instance_id)
    );

    CREATE INDEX IF NOT EXISTS webhook_event_deliveries_pending_idx
        ON webhook_event_deliveries (instance_id, leased_until, created_at)
        WHERE acknowledged_at IS NULL;

    ALTER TABLE setup_oauth_states
        ADD COLUMN IF NOT EXISTS connect_session_id UUID;

    ALTER TABLE connect_sessions
        ADD COLUMN IF NOT EXISTS authorized_github_user_id BIGINT;

    ALTER TABLE connect_sessions
        ADD COLUMN IF NOT EXISTS authorized_github_login TEXT;

    BEGIN
        ALTER TABLE setup_oauth_states
            ALTER COLUMN installation_id DROP NOT NULL;
    EXCEPTION
        WHEN others THEN NULL;
    END;
END;
$schema$;
