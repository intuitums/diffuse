ALTER TABLE review_runs
    ADD COLUMN verifier_model TEXT,
    ADD COLUMN provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN model_routing_reason TEXT NOT NULL DEFAULT 'legacy_single_model';

ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_provenance_object
        CHECK (
            jsonb_typeof(provenance) = 'object'
            AND octet_length(provenance::text) <= 65536
        ),
    ADD CONSTRAINT review_runs_model_routing_reason
        CHECK (model_routing_reason ~ '^[a-z0-9_]{1,64}$');
