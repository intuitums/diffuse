CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS repositories (
    id             BIGSERIAL PRIMARY KEY,
    scm_provider   TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    scm_base_url   TEXT NOT NULL,
    full_name      TEXT NOT NULL,
    default_branch TEXT,
    clone_url      TEXT,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    mirror_state   TEXT NOT NULL DEFAULT 'unconfigured'
                        CHECK (
                            mirror_state IN (
                                'unconfigured',
                                'syncing',
                                'ready',
                                'failed'
                            )
                        ),
    last_fetched_sha TEXT,
    last_fetched_at  TIMESTAMPTZ,
    last_error_code  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scm_provider, scm_base_url, full_name)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE
                         CHECK (length(name) BETWEEN 1 AND 100),
    token_sha256     TEXT NOT NULL UNIQUE
                         CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    scopes           TEXT[] NOT NULL
                         CHECK (
                             cardinality(scopes) BETWEEN 1 AND 8
                             AND scopes <@ ARRAY[
                                 'diffuse:mcp:read',
                                 'diffuse:mcp:write',
                                 'diffuse:mcp:generate',
                                 'diffuse:api:read',
                                 'diffuse:api:write',
                                 'diffuse:admin'
                             ]::TEXT[]
                         ),
    all_repositories BOOLEAN NOT NULL DEFAULT FALSE,
    created_by       TEXT NOT NULL
                         CHECK (length(created_by) BETWEEN 1 AND 255),
    expires_at       TIMESTAMPTZ,
    last_used_at     TIMESTAMPTZ,
    revoked_at       TIMESTAMPTZ,
    revoked_by       TEXT
                         CHECK (
                             revoked_by IS NULL
                             OR length(revoked_by) BETWEEN 1 AND 255
                         ),
    revocation_reason TEXT
                         CHECK (
                             revocation_reason IS NULL
                             OR length(revocation_reason) BETWEEN 1 AND 1000
                         ),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (
        (
            revoked_at IS NULL
            AND revoked_by IS NULL
            AND revocation_reason IS NULL
        )
        OR (
            revoked_at IS NOT NULL
            AND revoked_by IS NOT NULL
            AND revocation_reason IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS api_tokens_active_idx
    ON api_tokens (id)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS api_token_repositories (
    api_token_id  BIGINT NOT NULL
                      REFERENCES api_tokens (id) ON DELETE CASCADE,
    repository_id BIGINT NOT NULL
                      REFERENCES repositories (id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (api_token_id, repository_id)
);

CREATE INDEX IF NOT EXISTS api_token_repositories_repository_idx
    ON api_token_repositories (repository_id, api_token_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id             BIGSERIAL PRIMARY KEY,
    actor_kind     TEXT NOT NULL
                       CHECK (
                           actor_kind IN (
                               'operator',
                               'service_token',
                               'system'
                           )
                       ),
    actor_label    TEXT NOT NULL
                       CHECK (length(actor_label) BETWEEN 1 AND 255),
    action         TEXT NOT NULL
                       CHECK (action ~ '^[a-z][a-z0-9_.]{0,99}$'),
    resource_kind  TEXT NOT NULL
                       CHECK (resource_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    resource_id    TEXT NOT NULL
                       CHECK (length(resource_id) BETWEEN 1 AND 255),
    repository_id  BIGINT REFERENCES repositories (id) ON DELETE SET NULL,
    details        JSONB NOT NULL DEFAULT '{}'::JSONB
                       CHECK (jsonb_typeof(details) = 'object'),
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_events_resource_idx
    ON audit_events (resource_kind, resource_id, id DESC);

CREATE INDEX IF NOT EXISTS audit_events_repository_idx
    ON audit_events (repository_id, id DESC)
    WHERE repository_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS custom_contexts (
    id                  BIGSERIAL PRIMARY KEY,
    repository_id       BIGINT NOT NULL
                            REFERENCES repositories (id) ON DELETE CASCADE,
    context_type        TEXT NOT NULL
                            CHECK (
                                context_type IN (
                                    'CUSTOM_INSTRUCTION',
                                    'PATTERN'
                                )
                            ),
    body                TEXT NOT NULL
                            CHECK (length(body) BETWEEN 1 AND 12000),
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (
                                status IN (
                                    'active',
                                    'inactive',
                                    'suggested'
                                )
                            ),
    applies_to          TEXT[] NOT NULL DEFAULT ARRAY['**']::TEXT[]
                            CHECK (cardinality(applies_to) BETWEEN 1 AND 32),
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB
                            CHECK (jsonb_typeof(metadata) = 'object'),
    created_by_token_id BIGINT
                            REFERENCES api_tokens (id) ON DELETE SET NULL,
    created_by          TEXT NOT NULL
                            CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS custom_contexts_repository_status_idx
    ON custom_contexts (repository_id, status, id DESC);

CREATE TABLE IF NOT EXISTS repository_clusters (
    id             BIGSERIAL PRIMARY KEY,
    scm_provider   TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    scm_base_url   TEXT NOT NULL,
    name           TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    description    TEXT CHECK (
                       description IS NULL
                       OR length(description) BETWEEN 1 AND 500
                   ),
    created_by     TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scm_provider, scm_base_url, name)
);

CREATE TABLE IF NOT EXISTS repository_cluster_members (
    cluster_id     BIGINT NOT NULL
                       REFERENCES repository_clusters (id) ON DELETE CASCADE,
    repository_id  BIGINT NOT NULL
                       REFERENCES repositories (id) ON DELETE CASCADE,
    added_by       TEXT NOT NULL CHECK (length(added_by) BETWEEN 1 AND 255),
    added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, repository_id)
);

CREATE INDEX IF NOT EXISTS repository_cluster_members_repository_idx
    ON repository_cluster_members (repository_id, cluster_id);

CREATE TABLE IF NOT EXISTS index_snapshots (
    id                   BIGSERIAL PRIMARY KEY,
    repository_id        BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    commit_sha           TEXT NOT NULL CHECK (length(commit_sha) >= 7),
    status               TEXT NOT NULL
                             CHECK (status IN ('building', 'active', 'failed', 'superseded')),
    index_format_version TEXT NOT NULL,
    policy_fingerprint   TEXT NOT NULL CHECK (length(policy_fingerprint) = 64),
    embedding_model      TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions = 1536),
    failure_code         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS index_snapshots_one_active_per_repo_idx
    ON index_snapshots (repository_id)
    WHERE status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS index_snapshots_one_build_per_revision_idx
    ON index_snapshots (
        repository_id,
        commit_sha,
        index_format_version,
        embedding_model,
        embedding_dimensions
    )
    WHERE status = 'building';

CREATE INDEX IF NOT EXISTS index_snapshots_repository_created_idx
    ON index_snapshots (repository_id, id DESC);

CREATE TABLE IF NOT EXISTS code_chunks (
    id           BIGSERIAL PRIMARY KEY,
    snapshot_id  BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    file_path    TEXT NOT NULL,
    symbol_name  TEXT,
    start_line   INTEGER NOT NULL CHECK (start_line > 0),
    end_line     INTEGER NOT NULL CHECK (end_line >= start_line),
    content_hash TEXT NOT NULL,
    content      TEXT NOT NULL,
    embedding    VECTOR(1536) NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(
            to_tsvector('simple'::regconfig, coalesce(file_path, '')),
            'A'
        )
        ||
        setweight(
            to_tsvector('simple'::regconfig, coalesce(symbol_name, '')),
            'A'
        )
        ||
        setweight(
            to_tsvector('simple'::regconfig, coalesce(content, '')),
            'B'
        )
    ) STORED,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, file_path, start_line, end_line)
);

CREATE INDEX IF NOT EXISTS code_chunks_embedding_hnsw_idx
    ON code_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS code_chunks_search_vector_gin_idx
    ON code_chunks USING gin (search_vector);

CREATE INDEX IF NOT EXISTS code_chunks_snapshot_file_idx
    ON code_chunks (snapshot_id, file_path, start_line, end_line);

CREATE TABLE IF NOT EXISTS code_symbols (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_id    BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    stable_key     TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    language       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line     INTEGER NOT NULL CHECK (start_line > 0),
    end_line       INTEGER NOT NULL CHECK (end_line >= start_line),
    signature      TEXT,
    docstring      TEXT,
    content_hash   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, stable_key)
);

CREATE INDEX IF NOT EXISTS code_symbols_snapshot_qualified_name_idx
    ON code_symbols (snapshot_id, qualified_name);

CREATE INDEX IF NOT EXISTS code_symbols_snapshot_file_path_idx
    ON code_symbols (snapshot_id, file_path, start_line, end_line);

CREATE TABLE IF NOT EXISTS code_relationships (
    id                    BIGSERIAL PRIMARY KEY,
    snapshot_id           BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    source_symbol_key     TEXT NOT NULL,
    target_symbol_key     TEXT,
    target_qualified_name TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    line                  INTEGER CHECK (line IS NULL OR line > 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (snapshot_id, source_symbol_key)
        REFERENCES code_symbols (snapshot_id, stable_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS code_relationships_source_idx
    ON code_relationships (snapshot_id, source_symbol_key, kind);

CREATE INDEX IF NOT EXISTS code_relationships_target_idx
    ON code_relationships (snapshot_id, target_symbol_key, kind)
    WHERE target_symbol_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS code_relationships_target_name_idx
    ON code_relationships (snapshot_id, target_qualified_name, kind);

CREATE TABLE IF NOT EXISTS repository_policy_layers (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_id    BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    directory_path TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    config         JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, source_path)
);

CREATE INDEX IF NOT EXISTS repository_policy_layers_scope_idx
    ON repository_policy_layers (snapshot_id, directory_path);

CREATE TABLE IF NOT EXISTS repository_guidance_documents (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_id    BIGINT NOT NULL REFERENCES index_snapshots (id) ON DELETE CASCADE,
    directory_path TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('instructions', 'rules', 'context')),
    applies_to     TEXT[] NOT NULL CHECK (cardinality(applies_to) > 0),
    description    TEXT,
    content        TEXT NOT NULL,
    content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
    priority       INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, directory_path, source_path, kind, applies_to)
);

CREATE INDEX IF NOT EXISTS repository_guidance_documents_scope_idx
    ON repository_guidance_documents (snapshot_id, directory_path, priority DESC);

CREATE TABLE IF NOT EXISTS pull_requests (
    id               BIGSERIAL PRIMARY KEY,
    repository_id    BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    number           INTEGER NOT NULL CHECK (number > 0),
    web_url          TEXT NOT NULL,
    base_sha         TEXT NOT NULL CHECK (length(base_sha) >= 7),
    head_sha         TEXT NOT NULL CHECK (length(head_sha) >= 7),
    author           TEXT NOT NULL,
    base_branch      TEXT NOT NULL,
    head_branch      TEXT NOT NULL,
    is_draft         BOOLEAN NOT NULL DEFAULT FALSE,
    labels           TEXT[] NOT NULL DEFAULT '{}',
    title            TEXT NOT NULL,
    description      TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'open'
                         CHECK (state IN ('open', 'closed', 'merged')),
    changed_file_count INTEGER NOT NULL DEFAULT 0
                           CHECK (changed_file_count BETWEEN 0 AND 1000000),
    additions        INTEGER NOT NULL DEFAULT 0
                         CHECK (additions BETWEEN 0 AND 100000000),
    deletions        INTEGER NOT NULL DEFAULT 0
                         CHECK (deletions BETWEEN 0 AND 100000000),
    source_created_at TIMESTAMPTZ,
    source_closed_at TIMESTAMPTZ,
    source_merged_at TIMESTAMPTZ,
    latest_event_at  TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, number)
);

CREATE TABLE IF NOT EXISTS repository_refs (
    id               BIGSERIAL PRIMARY KEY,
    repository_id    BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    ref_name         TEXT NOT NULL,
    commit_sha       TEXT NOT NULL CHECK (length(commit_sha) >= 7),
    latest_event_at  TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, ref_name)
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id                BIGSERIAL PRIMARY KEY,
    repository_id     BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    pull_request_id   BIGINT REFERENCES pull_requests (id) ON DELETE CASCADE,
    job_type          TEXT NOT NULL
                           CHECK (
                               job_type IN (
                                   'review_pull_request',
                                   'answer_review_comment',
                                   'sync_review_feedback',
                                   'generate_suggested_rules',
                                   'index_repository'
                               )
                           ),
    idempotency_key   TEXT NOT NULL UNIQUE,
    scope_key         TEXT NOT NULL,
    base_revision     TEXT NOT NULL CHECK (length(base_revision) >= 7),
    revision          TEXT NOT NULL CHECK (length(revision) >= 7),
    status            TEXT NOT NULL DEFAULT 'queued'
                          CHECK (
                              status IN (
                                  'queued',
                                  'running',
                                  'succeeded',
                                  'failed',
                                  'dead',
                                  'cancelled',
                                  'superseded'
                              )
                          ),
    payload           JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    priority          SMALLINT NOT NULL DEFAULT 0,
    attempt_count     INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts      INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    available_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    leased_by         TEXT,
    lease_expires_at  TIMESTAMPTZ,
    last_error_code   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    CHECK (
        (
            job_type IN (
                'review_pull_request',
                'answer_review_comment',
                'sync_review_feedback'
            )
            AND pull_request_id IS NOT NULL
        )
        OR (
            job_type IN ('index_repository', 'generate_suggested_rules')
            AND pull_request_id IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS workflow_jobs_claim_idx
    ON workflow_jobs (priority DESC, available_at, id)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS workflow_jobs_scope_idx
    ON workflow_jobs (scope_key, id DESC);

CREATE TABLE IF NOT EXISTS workflow_attempts (
    id              BIGSERIAL PRIMARY KEY,
    workflow_job_id BIGINT NOT NULL REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL CHECK (attempt_number > 0),
    worker_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running', 'succeeded', 'failed', 'lease_expired')),
    error_code      TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    UNIQUE (workflow_job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS review_runs (
    id                     BIGSERIAL PRIMARY KEY,
    workflow_job_id        BIGINT NOT NULL UNIQUE
                               REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    repository_id          BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    pull_request_id        BIGINT NOT NULL REFERENCES pull_requests (id) ON DELETE CASCADE,
    index_snapshot_id      BIGINT REFERENCES index_snapshots (id) ON DELETE SET NULL,
    base_sha               TEXT NOT NULL CHECK (length(base_sha) >= 7),
    head_sha               TEXT NOT NULL CHECK (length(head_sha) >= 7),
    model                  TEXT NOT NULL,
    prompt_version         TEXT NOT NULL,
    context_fingerprint    TEXT NOT NULL CHECK (length(context_fingerprint) = 64),
    status                 TEXT NOT NULL
                                CHECK (
                                    status IN (
                                        'generating',
                                        'ready',
                                        'skipped',
                                        'publishing',
                                        'published',
                                        'failed',
                                        'superseded'
                                    )
                                ),
    summary                TEXT,
    risk_score             NUMERIC(4, 2)
                                CHECK (risk_score IS NULL OR risk_score BETWEEN 0 AND 10),
    confidence_score       SMALLINT
                                CHECK (
                                    confidence_score IS NULL
                                    OR confidence_score BETWEEN 0 AND 5
                                ),
    diagram_kind           TEXT
                                CHECK (
                                    diagram_kind IS NULL
                                    OR diagram_kind IN (
                                        'sequence',
                                        'entity_relation',
                                        'class',
                                        'flow'
                                    )
                                ),
    diagram_title          TEXT
                                CHECK (
                                    diagram_title IS NULL
                                    OR length(diagram_title) BETWEEN 1 AND 120
                                ),
    diagram_mermaid        TEXT
                                CHECK (
                                    diagram_mermaid IS NULL
                                    OR length(diagram_mermaid) BETWEEN 1 AND 12000
                                ),
    diagram_collapsible    BOOLEAN NOT NULL DEFAULT TRUE,
    diagram_default_open   BOOLEAN NOT NULL DEFAULT TRUE,
    summary_section_included BOOLEAN NOT NULL DEFAULT TRUE,
    summary_section_collapsible BOOLEAN NOT NULL DEFAULT FALSE,
    summary_section_default_open BOOLEAN NOT NULL DEFAULT TRUE,
    issues_table_section_included BOOLEAN NOT NULL DEFAULT TRUE,
    issues_table_section_collapsible BOOLEAN NOT NULL DEFAULT FALSE,
    issues_table_section_default_open BOOLEAN NOT NULL DEFAULT TRUE,
    confidence_score_section_included BOOLEAN NOT NULL DEFAULT TRUE,
    confidence_score_section_collapsible BOOLEAN NOT NULL DEFAULT FALSE,
    confidence_score_section_default_open BOOLEAN NOT NULL DEFAULT TRUE,
    footer_included        BOOLEAN NOT NULL DEFAULT TRUE,
    update_description     BOOLEAN NOT NULL DEFAULT FALSE,
    summary_comment_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    fix_with_agent_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    review_number          INTEGER CHECK (review_number IS NULL OR review_number > 0),
    context_chunk_count    INTEGER NOT NULL DEFAULT 0 CHECK (context_chunk_count >= 0),
    diff_file_count        INTEGER NOT NULL DEFAULT 0 CHECK (diff_file_count >= 0),
    reviewed_file_count    INTEGER NOT NULL DEFAULT 0 CHECK (reviewed_file_count >= 0),
    ignored_file_count     INTEGER NOT NULL DEFAULT 0 CHECK (ignored_file_count >= 0),
    inline_comments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    publication_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    skip_reason            TEXT CHECK (
                               skip_reason IS NULL
                               OR skip_reason ~ '^[a-z0-9_]{1,64}$'
                           ),
    prompt_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    failure_code           TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at               TIMESTAMPTZ,
    published_at           TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (
        pull_request_id,
        base_sha,
        head_sha,
        model,
        prompt_version,
        context_fingerprint
    ),
    UNIQUE (pull_request_id, review_number),
    CHECK (
        (diagram_kind IS NULL AND diagram_title IS NULL AND diagram_mermaid IS NULL)
        OR (
            diagram_kind IS NOT NULL
            AND diagram_title IS NOT NULL
            AND diagram_mermaid IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS review_runs_pull_request_idx
    ON review_runs (pull_request_id, id DESC);

CREATE TABLE IF NOT EXISTS review_run_context_snapshots (
    review_run_id         BIGINT NOT NULL
                              REFERENCES review_runs (id) ON DELETE CASCADE,
    repository_id        BIGINT NOT NULL,
    repository_full_name TEXT NOT NULL,
    snapshot_id          BIGINT NOT NULL,
    commit_sha           TEXT NOT NULL CHECK (length(commit_sha) >= 7),
    relation_kind        TEXT NOT NULL
                              CHECK (
                                  relation_kind IN (
                                      'explicit',
                                      'cluster',
                                      'explicit+cluster'
                                  )
                              ),
    cluster_ids          BIGINT[] NOT NULL DEFAULT '{}',
    ordinal              SMALLINT NOT NULL CHECK (ordinal BETWEEN 1 AND 7),
    PRIMARY KEY (review_run_id, repository_id),
    UNIQUE (review_run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS review_check_runs (
    id              BIGSERIAL PRIMARY KEY,
    review_run_id   BIGINT NOT NULL UNIQUE
                        REFERENCES review_runs (id) ON DELETE CASCADE,
    scm_provider    TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    head_sha        TEXT NOT NULL CHECK (length(head_sha) >= 7),
    check_name      TEXT NOT NULL,
    external_key    TEXT NOT NULL UNIQUE,
    external_id     TEXT,
    external_url    TEXT,
    status          TEXT NOT NULL
                        CHECK (
                            status IN (
                                'pending',
                                'creating',
                                'in_progress',
                                'completing',
                                'completed',
                                'failed'
                            )
                        ),
    conclusion      TEXT CHECK (
                        conclusion IS NULL
                        OR conclusion IN (
                            'cancelled',
                            'failure',
                            'neutral',
                            'skipped',
                            'success'
                        )
                    ),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    error_code      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'completed' AND conclusion IS NOT NULL)
        OR (status <> 'completed' AND conclusion IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS review_check_runs_status_idx
    ON review_check_runs (status, updated_at);

CREATE TABLE IF NOT EXISTS finding_lineages (
    id                        BIGSERIAL PRIMARY KEY,
    pull_request_id           BIGINT NOT NULL
                                  REFERENCES pull_requests (id) ON DELETE CASCADE,
    initial_fingerprint       TEXT NOT NULL CHECK (length(initial_fingerprint) = 64),
    status                    TEXT NOT NULL
                                  CHECK (status IN ('pending', 'active', 'addressed')),
    first_seen_review_run_id  BIGINT
                                  REFERENCES review_runs (id) ON DELETE SET NULL,
    last_seen_review_run_id   BIGINT
                                  REFERENCES review_runs (id) ON DELETE SET NULL,
    addressed_review_run_id   BIGINT
                                  REFERENCES review_runs (id) ON DELETE SET NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pull_request_id, initial_fingerprint),
    CHECK (
        (status IN ('pending', 'active') AND addressed_review_run_id IS NULL)
        OR (status = 'addressed' AND addressed_review_run_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS finding_lineages_pull_request_status_idx
    ON finding_lineages (pull_request_id, status, id);

CREATE TABLE IF NOT EXISTS review_findings (
    id                 BIGSERIAL PRIMARY KEY,
    review_run_id      BIGINT NOT NULL REFERENCES review_runs (id) ON DELETE CASCADE,
    lineage_id         BIGINT NOT NULL
                           REFERENCES finding_lineages (id) ON DELETE CASCADE,
    fingerprint        TEXT NOT NULL CHECK (length(fingerprint) = 64),
    ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
    title              TEXT NOT NULL,
    body               TEXT NOT NULL,
    severity           TEXT NOT NULL
                             CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    category           TEXT NOT NULL
                             CHECK (
                                 category IN (
                                     'correctness',
                                     'security',
                                     'performance',
                                     'reliability',
                                     'testing',
                                     'architecture',
                                     'maintainability',
                                     'api'
                                 )
                             ),
    security_classification TEXT
                             CHECK (
                                 security_classification IS NULL
                                 OR security_classification IN (
                                     'vulnerability',
                                     'preventative'
                                 )
                             ),
    confidence         NUMERIC(5, 4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    file_path          TEXT NOT NULL,
    line               INTEGER NOT NULL CHECK (line > 0),
    side               TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT')),
    evidence           TEXT NOT NULL,
    suggested_fix      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (review_run_id, fingerprint),
    UNIQUE (review_run_id, ordinal),
    CHECK (
        (
            category = 'security'
            AND security_classification IS NOT NULL
        )
        OR (
            category <> 'security'
            AND security_classification IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS review_findings_location_idx
    ON review_findings (review_run_id, file_path, line);

CREATE TABLE IF NOT EXISTS finding_lineage_events (
    id              BIGSERIAL PRIMARY KEY,
    lineage_id      BIGINT NOT NULL
                        REFERENCES finding_lineages (id) ON DELETE CASCADE,
    review_run_id   BIGINT NOT NULL REFERENCES review_runs (id) ON DELETE CASCADE,
    finding_id      BIGINT REFERENCES review_findings (id) ON DELETE SET NULL,
    transition      TEXT NOT NULL
                        CHECK (
                            transition IN (
                                'new',
                                'persistent',
                                'reopened',
                                'addressed'
                            )
                        ),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at      TIMESTAMPTZ,
    UNIQUE (lineage_id, review_run_id),
    CHECK (
        (transition = 'addressed' AND finding_id IS NULL)
        OR (transition <> 'addressed' AND finding_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS finding_lineage_events_review_run_idx
    ON finding_lineage_events (review_run_id, id);

CREATE TABLE IF NOT EXISTS finding_threads (
    id                    BIGSERIAL PRIMARY KEY,
    lineage_id            BIGINT NOT NULL UNIQUE
                              REFERENCES finding_lineages (id) ON DELETE CASCADE,
    scm_provider          TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    root_comment_id       TEXT NOT NULL,
    root_comment_node_id  TEXT,
    thread_node_id        TEXT,
    status                TEXT NOT NULL CHECK (status IN ('active', 'addressed')),
    created_review_run_id BIGINT
                              REFERENCES review_runs (id) ON DELETE SET NULL,
    last_synced_event_id  BIGINT
                              REFERENCES finding_lineage_events (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finding_threads_provider_root_idx
    ON finding_threads (scm_provider, root_comment_id);

CREATE TABLE IF NOT EXISTS review_feedback_sync_states (
    finding_thread_id      BIGINT PRIMARY KEY
                               REFERENCES finding_threads (id) ON DELETE CASCADE,
    generation             INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    next_sync_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_scheduled_job_id  BIGINT
                               REFERENCES workflow_jobs (id) ON DELETE SET NULL,
    last_started_at        TIMESTAMPTZ,
    last_completed_at      TIMESTAMPTZ,
    last_error_code        TEXT,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_feedback_sync_states_due_idx
    ON review_feedback_sync_states (next_sync_at, finding_thread_id);

CREATE TABLE IF NOT EXISTS review_feedback_events (
    id                     BIGSERIAL PRIMARY KEY,
    repository_id          BIGINT NOT NULL
                               REFERENCES repositories (id) ON DELETE CASCADE,
    pull_request_id        BIGINT NOT NULL
                               REFERENCES pull_requests (id) ON DELETE CASCADE,
    finding_thread_id      BIGINT NOT NULL
                               REFERENCES finding_threads (id) ON DELETE CASCADE,
    finding_id             BIGINT
                               REFERENCES review_findings (id) ON DELETE SET NULL,
    scm_provider           TEXT NOT NULL
                               CHECK (scm_provider IN ('github', 'gitlab')),
    source_kind            TEXT NOT NULL
                               CHECK (
                                   source_kind IN (
                                       'reaction',
                                       'reply',
                                       'commit_outcome'
                                   )
                               ),
    signal_kind            TEXT NOT NULL
                               CHECK (
                                   signal_kind IN (
                                       'positive',
                                       'negative',
                                       'context',
                                       'addressed',
                                       'reopened'
                                   )
                               ),
    event_action           TEXT NOT NULL
                               CHECK (event_action IN ('observed', 'withdrawn')),
    event_key              TEXT NOT NULL,
    source_external_id     TEXT NOT NULL,
    source_comment_id      TEXT NOT NULL,
    source_delivery_id     TEXT,
    source_payload_sha256  TEXT
                               CHECK (
                                   source_payload_sha256 IS NULL
                                   OR length(source_payload_sha256) = 64
                               ),
    actor_login            TEXT,
    actor_authority        TEXT
                               CHECK (
                                   actor_authority IS NULL
                                   OR actor_authority IN (
                                       'OWNER',
                                       'MEMBER',
                                       'COLLABORATOR',
                                       'REPOSITORY_COLLABORATOR'
                                   )
                               ),
    content                TEXT CHECK (content IS NULL OR length(content) <= 65536),
    finding_category       TEXT NOT NULL
                               CHECK (
                                   finding_category IN (
                                       'correctness',
                                       'security',
                                       'performance',
                                       'reliability',
                                       'testing',
                                       'architecture',
                                       'maintainability',
                                       'api'
                                   )
                               ),
    finding_security_classification TEXT
                               CHECK (
                                   finding_security_classification IS NULL
                                   OR finding_security_classification IN (
                                       'vulnerability',
                                       'preventative'
                                   )
                               ),
    finding_severity       TEXT NOT NULL
                               CHECK (
                                   finding_severity IN (
                                       'critical',
                                       'high',
                                       'medium',
                                       'low'
                                   )
                               ),
    suppression_protected  BOOLEAN GENERATED ALWAYS AS (
                               finding_category IN ('correctness', 'security')
                               OR finding_severity = 'critical'
                           ) STORED,
    source_created_at      TIMESTAMPTZ,
    observed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, event_key),
    CHECK (
        (
            finding_category = 'security'
            AND finding_security_classification IS NOT NULL
        )
        OR (
            finding_category <> 'security'
            AND finding_security_classification IS NULL
        )
    ),
    CHECK (
        (source_kind = 'reaction' AND content IN ('+1', '-1')
            AND actor_login IS NOT NULL)
        OR source_kind <> 'reaction'
    ),
    CHECK (
        (source_kind = 'reply' AND signal_kind = 'context'
            AND event_action = 'observed' AND content IS NOT NULL)
        OR source_kind <> 'reply'
    ),
    CHECK (
        (source_kind = 'commit_outcome'
            AND signal_kind IN ('addressed', 'reopened')
            AND event_action = 'observed')
        OR source_kind <> 'commit_outcome'
    )
);

CREATE INDEX IF NOT EXISTS review_feedback_events_thread_idx
    ON review_feedback_events (finding_thread_id, id);

CREATE INDEX IF NOT EXISTS review_feedback_events_repository_idx
    ON review_feedback_events (repository_id, id);

CREATE TABLE IF NOT EXISTS suggested_rule_learning_states (
    repository_id             BIGINT PRIMARY KEY
                                  REFERENCES repositories (id) ON DELETE CASCADE,
    generation                INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    next_evaluation_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_evidence_fingerprint TEXT
                                  CHECK (
                                      last_evidence_fingerprint IS NULL
                                      OR length(last_evidence_fingerprint) = 64
                                  ),
    last_scheduled_job_id     BIGINT
                                  REFERENCES workflow_jobs (id) ON DELETE SET NULL,
    last_started_at           TIMESTAMPTZ,
    last_completed_at         TIMESTAMPTZ,
    last_error_code           TEXT,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS suggested_rule_learning_states_due_idx
    ON suggested_rule_learning_states (next_evaluation_at, repository_id);

CREATE TABLE IF NOT EXISTS suggested_rule_generation_runs (
    id                   BIGSERIAL PRIMARY KEY,
    workflow_job_id      BIGINT NOT NULL UNIQUE
                             REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    repository_id        BIGINT NOT NULL
                             REFERENCES repositories (id) ON DELETE CASCADE,
    evidence_fingerprint TEXT NOT NULL CHECK (length(evidence_fingerprint) = 64),
    model                TEXT NOT NULL,
    prompt_version       TEXT NOT NULL,
    status               TEXT NOT NULL
                             CHECK (status IN ('generating', 'ready', 'failed', 'stale')),
    evidence_count       INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    proposed_count       INTEGER NOT NULL DEFAULT 0 CHECK (proposed_count >= 0),
    consolidated_count   INTEGER NOT NULL DEFAULT 0 CHECK (consolidated_count >= 0),
    rejected_count       INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    prompt_tokens        INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens    INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    failure_code         TEXT,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS suggested_rule_generation_runs_repository_idx
    ON suggested_rule_generation_runs (repository_id, id DESC);

CREATE TABLE IF NOT EXISTS learned_rules (
    id                   BIGSERIAL PRIMARY KEY,
    repository_id        BIGINT NOT NULL
                             REFERENCES repositories (id) ON DELETE CASCADE,
    generated_run_id     BIGINT
                             REFERENCES suggested_rule_generation_runs (id)
                             ON DELETE SET NULL,
    deduplication_key    TEXT NOT NULL CHECK (length(deduplication_key) = 64),
    status               TEXT NOT NULL DEFAULT 'suggested'
                             CHECK (
                                 status IN (
                                     'suggested',
                                     'active',
                                     'inactive',
                                     'rejected'
                                 )
                             ),
    version              INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    title                TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    guidance             TEXT NOT NULL CHECK (length(guidance) BETWEEN 1 AND 6000),
    applies_to           TEXT[] NOT NULL DEFAULT ARRAY['**']::TEXT[]
                             CHECK (cardinality(applies_to) BETWEEN 1 AND 32),
    severity             TEXT NOT NULL
                             CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    category             TEXT NOT NULL
                             CHECK (
                                 category IN (
                                     'correctness',
                                     'security',
                                     'performance',
                                     'reliability',
                                     'testing',
                                     'architecture',
                                     'maintainability',
                                     'api'
                                 )
                             ),
    evidence_count       INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    activated_at         TIMESTAMPTZ,
    deactivated_at       TIMESTAMPTZ,
    rejected_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, deduplication_key)
);

CREATE INDEX IF NOT EXISTS learned_rules_repository_status_idx
    ON learned_rules (repository_id, status, id);

CREATE TABLE IF NOT EXISTS suggested_rule_evidence (
    learned_rule_id      BIGINT NOT NULL
                             REFERENCES learned_rules (id) ON DELETE CASCADE,
    feedback_event_id    BIGINT NOT NULL
                             REFERENCES review_feedback_events (id) ON DELETE CASCADE,
    generation_run_id    BIGINT NOT NULL
                             REFERENCES suggested_rule_generation_runs (id)
                             ON DELETE CASCADE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (learned_rule_id, feedback_event_id)
);

CREATE INDEX IF NOT EXISTS suggested_rule_evidence_feedback_idx
    ON suggested_rule_evidence (feedback_event_id, learned_rule_id);

CREATE TABLE IF NOT EXISTS learned_rule_events (
    id                   BIGSERIAL PRIMARY KEY,
    learned_rule_id      BIGINT NOT NULL
                             REFERENCES learned_rules (id) ON DELETE CASCADE,
    generation_run_id    BIGINT
                             REFERENCES suggested_rule_generation_runs (id)
                             ON DELETE SET NULL,
    action               TEXT NOT NULL
                             CHECK (
                                 action IN (
                                     'proposed',
                                     'evidence_added',
                                     'edited',
                                     'approved',
                                     'rejected',
                                     'deactivated',
                                     'reactivated'
                                 )
                             ),
    event_key            TEXT NOT NULL,
    actor_login          TEXT,
    actor_authority      TEXT
                             CHECK (
                                 actor_authority IS NULL
                                 OR actor_authority IN (
                                     'OWNER',
                                     'ADMIN',
                                     'MEMBER',
                                     'COLLABORATOR',
                                     'OPERATOR'
                                 )
                             ),
    reason               TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
    rule_version         INTEGER NOT NULL CHECK (rule_version > 0),
    snapshot             JSONB NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (learned_rule_id, event_key),
    CHECK (
        (action IN ('proposed', 'evidence_added') AND actor_login IS NULL)
        OR (
            action NOT IN ('proposed', 'evidence_added')
            AND actor_login IS NOT NULL
            AND actor_authority IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS learned_rule_events_rule_idx
    ON learned_rule_events (learned_rule_id, id);

CREATE TABLE IF NOT EXISTS review_run_learned_rules (
    review_run_id        BIGINT NOT NULL
                             REFERENCES review_runs (id) ON DELETE CASCADE,
    -- Deliberately retain the numeric identity in the immutable snapshot
    -- without a rule FK: deleting a rule must not erase past review provenance.
    learned_rule_id      BIGINT NOT NULL,
    rule_version         INTEGER NOT NULL CHECK (rule_version > 0),
    snapshot             JSONB NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    PRIMARY KEY (review_run_id, learned_rule_id)
);

CREATE TABLE IF NOT EXISTS review_run_custom_contexts (
    review_run_id        BIGINT NOT NULL
                             REFERENCES review_runs (id) ON DELETE CASCADE,
    custom_context_id    BIGINT NOT NULL,
    snapshot             JSONB NOT NULL
                             CHECK (jsonb_typeof(snapshot) = 'object'),
    PRIMARY KEY (review_run_id, custom_context_id)
);

CREATE TABLE IF NOT EXISTS review_conversation_messages (
    id                          BIGSERIAL PRIMARY KEY,
    workflow_job_id             BIGINT NOT NULL UNIQUE
                                    REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    pull_request_id             BIGINT NOT NULL
                                    REFERENCES pull_requests (id) ON DELETE CASCADE,
    finding_thread_id           BIGINT NOT NULL
                                    REFERENCES finding_threads (id) ON DELETE CASCADE,
    index_snapshot_id           BIGINT
                                    REFERENCES index_snapshots (id) ON DELETE SET NULL,
    scm_provider                TEXT NOT NULL
                                    CHECK (scm_provider IN ('github', 'gitlab')),
    external_comment_id         TEXT NOT NULL,
    root_comment_id             TEXT NOT NULL,
    author_login                TEXT NOT NULL,
    author_association          TEXT NOT NULL
                                    CHECK (
                                        author_association IN (
                                            'OWNER',
                                            'MEMBER',
                                            'COLLABORATOR'
                                        )
                                    ),
    question                    TEXT NOT NULL
                                    CHECK (length(question) BETWEEN 1 AND 12000),
    file_path                   TEXT NOT NULL,
    line                        INTEGER NOT NULL CHECK (line > 0),
    side                        TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT')),
    diff_hunk                   TEXT NOT NULL,
    comment_commit_sha          TEXT NOT NULL
                                    CHECK (length(comment_commit_sha) >= 7),
    base_sha                    TEXT NOT NULL CHECK (length(base_sha) >= 7),
    head_sha                    TEXT NOT NULL CHECK (length(head_sha) >= 7),
    model                       TEXT,
    prompt_version              TEXT,
    status                      TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (
                                        status IN (
                                            'pending',
                                            'generating',
                                            'ready',
                                            'publishing',
                                            'published',
                                            'failed',
                                            'ignored'
                                        )
                                    ),
    answer                      TEXT,
    code_references             JSONB NOT NULL DEFAULT '[]'
                                    CHECK (jsonb_typeof(code_references) = 'array'),
    context_chunk_count         INTEGER NOT NULL DEFAULT 0
                                    CHECK (context_chunk_count >= 0),
    prompt_tokens               INTEGER NOT NULL DEFAULT 0
                                    CHECK (prompt_tokens >= 0),
    completion_tokens           INTEGER NOT NULL DEFAULT 0
                                    CHECK (completion_tokens >= 0),
    publication_attempt_count   INTEGER NOT NULL DEFAULT 0
                                    CHECK (publication_attempt_count >= 0),
    external_reply_id           TEXT,
    external_reply_url          TEXT,
    error_code                  TEXT,
    source_created_at           TIMESTAMPTZ NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at                    TIMESTAMPTZ,
    published_at                TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pull_request_id, external_comment_id),
    CHECK (
        (status = 'published' AND answer IS NOT NULL
            AND external_reply_id IS NOT NULL AND published_at IS NOT NULL)
        OR status <> 'published'
    ),
    CHECK (
        status NOT IN ('ready', 'publishing')
        OR answer IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS review_conversation_messages_thread_idx
    ON review_conversation_messages (finding_thread_id, id);

CREATE INDEX IF NOT EXISTS review_conversation_messages_status_idx
    ON review_conversation_messages (status, updated_at);

CREATE TABLE IF NOT EXISTS finding_thread_operations (
    id                  BIGSERIAL PRIMARY KEY,
    lineage_event_id    BIGINT NOT NULL UNIQUE
                            REFERENCES finding_lineage_events (id) ON DELETE CASCADE,
    finding_thread_id   BIGINT NOT NULL
                            REFERENCES finding_threads (id) ON DELETE CASCADE,
    operation_kind      TEXT NOT NULL CHECK (operation_kind IN ('address', 'reopen')),
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL
                            CHECK (
                                status IN (
                                    'pending',
                                    'publishing',
                                    'published',
                                    'failed'
                                )
                            ),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    external_reply_id   TEXT,
    external_reply_url  TEXT,
    error_code          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finding_thread_operations_status_idx
    ON finding_thread_operations (status, updated_at);

CREATE TABLE IF NOT EXISTS review_auto_approvals (
    id                  BIGSERIAL PRIMARY KEY,
    review_run_id       BIGINT NOT NULL UNIQUE
                            REFERENCES review_runs (id) ON DELETE CASCADE,
    scm_provider        TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    head_sha            TEXT NOT NULL CHECK (length(head_sha) >= 7),
    policy_fingerprint  TEXT NOT NULL CHECK (length(policy_fingerprint) = 64),
    eligible            BOOLEAN NOT NULL,
    decision_reason     TEXT NOT NULL
                            CHECK (decision_reason ~ '^[a-z0-9_]{1,64}$'),
    risk_level          TEXT NOT NULL
                            CHECK (
                                risk_level IN (
                                    'low',
                                    'medium',
                                    'high',
                                    'critical'
                                )
                            ),
    risk_ceiling        TEXT NOT NULL
                            CHECK (
                                risk_ceiling IN (
                                    'low',
                                    'medium',
                                    'high',
                                    'critical'
                                )
                            ),
    changed_paths       TEXT[] NOT NULL,
    changed_file_count  INTEGER NOT NULL CHECK (changed_file_count > 0),
    changed_line_count  INTEGER NOT NULL CHECK (changed_line_count >= 0),
    diff_chars          INTEGER NOT NULL CHECK (diff_chars > 0),
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL
                            CHECK (
                                status IN (
                                    'ineligible',
                                    'publishing',
                                    'published',
                                    'failed',
                                    'cancelled'
                                )
                            ),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    external_id         TEXT,
    external_url        TEXT,
    error_code          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (eligible AND status <> 'ineligible')
        OR (NOT eligible AND status = 'ineligible')
    )
);

CREATE TABLE IF NOT EXISTS review_publications (
    id                BIGSERIAL PRIMARY KEY,
    review_run_id     BIGINT NOT NULL REFERENCES review_runs (id) ON DELETE CASCADE,
    scm_provider      TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    publication_kind  TEXT NOT NULL CHECK (publication_kind IN ('pull_request_review')),
    idempotency_key   TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL
                           CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    attempt_count     INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    external_id       TEXT,
    external_url      TEXT,
    error_code        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (review_run_id, scm_provider, publication_kind)
);

CREATE TABLE IF NOT EXISTS scm_webhook_deliveries (
    id               BIGSERIAL PRIMARY KEY,
    scm_provider     TEXT NOT NULL CHECK (scm_provider IN ('github', 'gitlab')),
    scm_base_url     TEXT NOT NULL,
    delivery_id      TEXT NOT NULL,
    event_name       TEXT NOT NULL,
    payload_sha256   TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    workflow_job_id  BIGINT REFERENCES workflow_jobs (id) ON DELETE SET NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scm_provider, scm_base_url, delivery_id)
);

CREATE TABLE IF NOT EXISTS pull_request_lifecycle_events (
    id                        BIGSERIAL PRIMARY KEY,
    pull_request_id           BIGINT NOT NULL
                                  REFERENCES pull_requests (id) ON DELETE CASCADE,
    scm_webhook_delivery_id   BIGINT NOT NULL UNIQUE
                                  REFERENCES scm_webhook_deliveries (id)
                                  ON DELETE CASCADE,
    action                    TEXT NOT NULL
                                  CHECK (action ~ '^[a-z][a-z0-9_]{0,63}$'),
    state                     TEXT NOT NULL
                                  CHECK (state IN ('open', 'closed', 'merged')),
    source_event_at           TIMESTAMPTZ NOT NULL,
    source_created_at         TIMESTAMPTZ,
    source_closed_at          TIMESTAMPTZ,
    source_merged_at          TIMESTAMPTZ,
    recorded_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (state = 'open' AND source_closed_at IS NULL AND source_merged_at IS NULL)
        OR (state = 'closed' AND source_merged_at IS NULL)
        OR state = 'merged'
    ),
    CHECK (
        source_created_at IS NULL
        OR (
            (source_closed_at IS NULL OR source_closed_at >= source_created_at)
            AND (source_merged_at IS NULL OR source_merged_at >= source_created_at)
        )
    )
);

CREATE INDEX IF NOT EXISTS pull_request_lifecycle_events_pr_idx
    ON pull_request_lifecycle_events (pull_request_id, source_event_at, id);

CREATE INDEX IF NOT EXISTS pull_request_lifecycle_events_event_idx
    ON pull_request_lifecycle_events (source_event_at, state, pull_request_id);
