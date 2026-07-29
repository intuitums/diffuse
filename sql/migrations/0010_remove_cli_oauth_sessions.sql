-- Remove the abandoned Diffuse CLI OAuth session flow.
--
-- GitHub browser authentication now exists only to attribute a shared GitHub
-- App installation before issuing a one-time relay pairing code. It never
-- redirects credentials to a local process and does not mint a Diffuse login
-- session. Pending OAuth state is ephemeral, so invalidating it during this
-- migration is safer than trying to translate an in-flight authorization.

DELETE FROM oauth_states;

DROP TABLE sessions;

ALTER TABLE oauth_states
    DROP CONSTRAINT oauth_states_purpose_check,
    -- CASCADE removes the two checks that refer to this obsolete column. Their
    -- PostgreSQL-generated names are deliberately not part of the migration.
    DROP COLUMN callback_port CASCADE;

ALTER TABLE oauth_states
    ADD CONSTRAINT oauth_states_purpose_check
        CHECK (purpose IN ('github_install_auth', 'app_install')),
    ADD CONSTRAINT oauth_states_shape_check
        CHECK (
            (purpose = 'github_install_auth' AND user_id IS NULL)
            OR (purpose = 'app_install' AND user_id IS NOT NULL)
        );
