-- Narrow every scm_provider CHECK to github-only (DEV-250 / ADR 0041).
-- The GitLab product path was removed in PR #30; these constraints still
-- admitted 'gitlab' so a historical row would not fail a cutover deploy.
-- Adding the narrowed CHECK fails closed if any non-github row remains —
-- operators should count scm_provider per table before applying:
--   SELECT scm_provider, count(*) FROM <table> GROUP BY 1;

ALTER TABLE repositories
    DROP CONSTRAINT repositories_scm_provider_check;
ALTER TABLE repositories
    ADD CONSTRAINT repositories_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE repository_clusters
    DROP CONSTRAINT repository_clusters_scm_provider_check;
ALTER TABLE repository_clusters
    ADD CONSTRAINT repository_clusters_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE review_check_runs
    DROP CONSTRAINT review_check_runs_scm_provider_check;
ALTER TABLE review_check_runs
    ADD CONSTRAINT review_check_runs_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE finding_threads
    DROP CONSTRAINT finding_threads_scm_provider_check;
ALTER TABLE finding_threads
    ADD CONSTRAINT finding_threads_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE review_feedback_events
    DROP CONSTRAINT review_feedback_events_scm_provider_check;
ALTER TABLE review_feedback_events
    ADD CONSTRAINT review_feedback_events_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE review_conversation_messages
    DROP CONSTRAINT review_conversation_messages_scm_provider_check;
ALTER TABLE review_conversation_messages
    ADD CONSTRAINT review_conversation_messages_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE review_auto_approvals
    DROP CONSTRAINT review_auto_approvals_scm_provider_check;
ALTER TABLE review_auto_approvals
    ADD CONSTRAINT review_auto_approvals_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE review_publications
    DROP CONSTRAINT review_publications_scm_provider_check;
ALTER TABLE review_publications
    ADD CONSTRAINT review_publications_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE scm_webhook_deliveries
    DROP CONSTRAINT scm_webhook_deliveries_scm_provider_check;
ALTER TABLE scm_webhook_deliveries
    ADD CONSTRAINT scm_webhook_deliveries_scm_provider_check
    CHECK (scm_provider IN ('github'));

ALTER TABLE scm_webhook_rejections
    DROP CONSTRAINT scm_webhook_rejections_scm_provider_check;
ALTER TABLE scm_webhook_rejections
    ADD CONSTRAINT scm_webhook_rejections_scm_provider_check
    CHECK (scm_provider IN ('github'));
