ALTER TABLE api_idempotency_keys
    ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 1
        CHECK (lease_generation > 0);
