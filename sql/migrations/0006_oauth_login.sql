-- Browser OAuth sign-in for the Diffuse CLI.
--
-- Diffuse is the confidential OAuth client: the GitHub client secret stays on
-- the server and the GitHub access token is never persisted. Only the identity
-- it proves is kept, plus a Diffuse-minted session token stored as SHA-256.
--
-- `oauth_states` is the CSRF ledger for both redirects. Rows are single-use
-- (claimed by setting consumed_at) and TTL-bounded, and only the SHA-256 of the
-- nonce is stored so a database read cannot forge a pending authorization.

CREATE TABLE IF NOT EXISTS users (
    id             BIGSERIAL PRIMARY KEY,
    github_user_id BIGINT NOT NULL UNIQUE
                       CHECK (github_user_id > 0),
    login          TEXT NOT NULL
                       CHECK (length(login) BETWEEN 1 AND 255),
    avatar_url     TEXT
                       CHECK (
                           avatar_url IS NULL
                           OR length(avatar_url) BETWEEN 1 AND 2048
                       ),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL
                     REFERENCES users (id) ON DELETE CASCADE,
    token_sha256 TEXT NOT NULL UNIQUE
                     CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS sessions_user_active_idx
    ON sessions (user_id, id DESC)
    WHERE revoked_at IS NULL;

-- A user may connect several GitHub organizations. `github_installation_id` is
-- stored directly rather than as a foreign key: Diffuse has no `installations`
-- table yet, and the tenancy work that introduces one can backfill from here.
CREATE TABLE IF NOT EXISTS user_installations (
    user_id                BIGINT NOT NULL
                               REFERENCES users (id) ON DELETE CASCADE,
    github_installation_id BIGINT NOT NULL
                               CHECK (github_installation_id > 0),
    account_login          TEXT
                               CHECK (
                                   account_login IS NULL
                                   OR length(account_login) BETWEEN 1 AND 255
                               ),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, github_installation_id)
);

CREATE INDEX IF NOT EXISTS user_installations_installation_idx
    ON user_installations (github_installation_id, user_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    id            BIGSERIAL PRIMARY KEY,
    state_sha256  TEXT NOT NULL UNIQUE
                      CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    purpose       TEXT NOT NULL
                      CHECK (purpose IN ('cli_login', 'app_install')),
    callback_port INTEGER
                      CHECK (
                          callback_port IS NULL
                          OR callback_port BETWEEN 1024 AND 65535
                      ),
    user_id       BIGINT
                      REFERENCES users (id) ON DELETE CASCADE,
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at),
    -- A CLI sign-in is anonymous until the callback resolves it; an app-install
    -- redirect is already bound to a signed-in user and never has a port.
    CHECK (
        (purpose = 'cli_login' AND user_id IS NULL)
        OR (purpose = 'app_install' AND user_id IS NOT NULL AND callback_port IS NULL)
    )
);

-- Not partial on `consumed_at IS NULL`: the only expiry-ordered query is the
-- opportunistic purge, which drains consumed and abandoned states alike. A
-- partial index cannot serve it, and `/auth/cli` is unauthenticated, so a
-- sequential scan there is a remotely triggerable amplification.
CREATE INDEX IF NOT EXISTS oauth_states_expires_at_idx
    ON oauth_states (expires_at);
