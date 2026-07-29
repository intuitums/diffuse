-- Hosted integration relay.
--
-- The relay owns public provider callbacks while review execution stays on an
-- operator-controlled Diffuse node. Pairing codes and node credentials are
-- stored only as SHA-256 digests. Payloads are retained only until the paired
-- node acknowledges durable local ingestion.

CREATE TABLE relay_github_installations (
    github_installation_id       BIGINT PRIMARY KEY
                                       CHECK (github_installation_id > 0),
    account_id                   BIGINT NOT NULL
                                       CHECK (account_id > 0),
    account_login                TEXT NOT NULL
                                       CHECK (length(account_login) BETWEEN 1 AND 255),
    account_type                 TEXT NOT NULL
                                       CHECK (account_type IN ('Organization', 'User')),
    installed_by_github_user_id  BIGINT NOT NULL
                                       CHECK (installed_by_github_user_id > 0),
    installed_by_login           TEXT NOT NULL
                                       CHECK (length(installed_by_login) BETWEEN 1 AND 255),
    status                       TEXT NOT NULL DEFAULT 'active'
                                       CHECK (
                                           status IN ('active', 'suspended', 'revoked')
                                       ),
    installed_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                   TIMESTAMPTZ,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status IN ('suspended', 'revoked') AND revoked_at IS NOT NULL)
    )
);

CREATE INDEX relay_github_installations_installer_idx
    ON relay_github_installations (installed_by_github_user_id, github_installation_id)
    WHERE status = 'active';

CREATE TABLE relay_pairing_codes (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                BIGINT NOT NULL
                                 REFERENCES users (id) ON DELETE CASCADE,
    github_installation_id BIGINT NOT NULL
                                 REFERENCES relay_github_installations (
                                     github_installation_id
                                 ) ON DELETE CASCADE
                                 CHECK (github_installation_id > 0),
    code_sha256            TEXT NOT NULL UNIQUE
                                 CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
    expires_at             TIMESTAMPTZ NOT NULL,
    consumed_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);

CREATE INDEX relay_pairing_codes_installation_active_idx
    ON relay_pairing_codes (github_installation_id, expires_at DESC)
    WHERE consumed_at IS NULL;

CREATE TABLE relay_nodes (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                BIGINT NOT NULL
                                 REFERENCES users (id) ON DELETE RESTRICT,
    github_installation_id BIGINT NOT NULL UNIQUE
                                 REFERENCES relay_github_installations (
                                     github_installation_id
                                 ) ON DELETE RESTRICT
                                 CHECK (github_installation_id > 0),
    name                   TEXT NOT NULL
                                 CHECK (length(name) BETWEEN 1 AND 255),
    token_sha256           TEXT NOT NULL UNIQUE
                                 CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    last_seen_at           TIMESTAMPTZ,
    revoked_at             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX relay_nodes_user_active_idx
    ON relay_nodes (user_id, id)
    WHERE revoked_at IS NULL;

CREATE TABLE relay_deliveries (
    id                     BIGSERIAL PRIMARY KEY,
    provider               TEXT NOT NULL
                                 CHECK (provider IN ('github', 'slack')),
    provider_delivery_id   TEXT NOT NULL
                                 CHECK (length(provider_delivery_id) BETWEEN 1 AND 255),
    event_name             TEXT NOT NULL
                                 CHECK (length(event_name) BETWEEN 1 AND 128),
    github_installation_id BIGINT
                                 CHECK (
                                     github_installation_id IS NULL
                                     OR github_installation_id > 0
                                 ),
    slack_team_id          TEXT
                                 CHECK (
                                     slack_team_id IS NULL
                                     OR length(slack_team_id) BETWEEN 2 AND 64
                                 ),
    payload                BYTEA,
    payload_sha256         TEXT NOT NULL
                                 CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    status                 TEXT NOT NULL DEFAULT 'queued'
                                 CHECK (
                                     status IN ('queued', 'leased', 'delivered', 'dead')
                                 ),
    leased_by_node_id      BIGINT
                                 REFERENCES relay_nodes (id) ON DELETE SET NULL,
    leased_until           TIMESTAMPTZ,
    attempt_count          INTEGER NOT NULL DEFAULT 0
                                 CHECK (attempt_count >= 0),
    last_error_code        TEXT
                                 CHECK (
                                     last_error_code IS NULL
                                     OR length(last_error_code) BETWEEN 1 AND 128
                                 ),
    received_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at           TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_delivery_id),
    CHECK (payload IS NULL OR octet_length(payload) <= 1000000),
    CHECK (
        (provider = 'github'
         AND github_installation_id IS NOT NULL
         AND slack_team_id IS NULL)
        OR
        (provider = 'slack'
         AND github_installation_id IS NULL
         AND slack_team_id IS NOT NULL)
    ),
    CHECK (
        (status = 'queued'
         AND leased_by_node_id IS NULL
         AND leased_until IS NULL
         AND delivered_at IS NULL
         AND payload IS NOT NULL)
        OR
        (status = 'leased'
         AND leased_by_node_id IS NOT NULL
         AND leased_until IS NOT NULL
         AND delivered_at IS NULL
         AND payload IS NOT NULL)
        OR
        (status = 'delivered'
         AND leased_by_node_id IS NOT NULL
         AND leased_until IS NULL
         AND delivered_at IS NOT NULL
         AND payload IS NULL)
        OR
        (status = 'dead'
         AND leased_until IS NULL)
    )
);

CREATE INDEX relay_deliveries_github_claim_idx
    ON relay_deliveries (
        github_installation_id,
        status,
        leased_until,
        received_at,
        id
    )
    WHERE provider = 'github' AND status IN ('queued', 'leased');

CREATE INDEX relay_deliveries_retention_idx
    ON relay_deliveries (delivered_at, id)
    WHERE status = 'delivered';
