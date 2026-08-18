CREATE TABLE api_idempotency_keys (
    id               BIGSERIAL PRIMARY KEY,
    actor_identity   TEXT NOT NULL
                         CHECK (length(actor_identity) BETWEEN 1 AND 255),
    operation        TEXT NOT NULL
                         CHECK (operation ~ '^[a-z][a-z0-9_.-]{0,99}$'),
    key_sha256       TEXT NOT NULL
                         CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256   TEXT NOT NULL
                         CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    state            TEXT NOT NULL DEFAULT 'processing'
                         CHECK (state IN ('processing', 'completed')),
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    operation_data   JSONB,
    response         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (actor_identity, operation, key_sha256),
    CHECK (
        (state = 'processing' AND response IS NULL)
        OR (state = 'completed' AND response IS NOT NULL)
    )
);

CREATE INDEX api_idempotency_keys_expired_idx
ON api_idempotency_keys (lease_expires_at)
WHERE state = 'processing';
