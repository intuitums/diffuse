ALTER TABLE repositories
    ADD COLUMN github_repository_id BIGINT
        CHECK (github_repository_id IS NULL OR github_repository_id > 0);

CREATE UNIQUE INDEX repositories_github_identity_idx
    ON repositories (scm_provider, scm_base_url, github_repository_id)
    WHERE github_repository_id IS NOT NULL;
