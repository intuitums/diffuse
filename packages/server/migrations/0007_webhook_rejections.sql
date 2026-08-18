-- Observability for webhook deliveries Diffuse refuses.
--
-- A delivery for a repository that was never onboarded is rejected before it
-- reaches the `scm_webhook_deliveries` insert, so until now it left no trace at
-- all: an operator could not tell whether GitHub was delivering and being
-- turned away, or never delivering in the first place.
--
-- This is deliberately a separate table rather than a column on
-- `scm_webhook_deliveries`. That table's UNIQUE (provider, base_url,
-- delivery_id) is what makes redelivery idempotent — recording a rejection
-- there would make GitHub's later retry look like a duplicate and silently
-- never enqueue, so onboarding the repository would not recover the event.
--
-- Rows are upserted per delivery, so a retried delivery increments `attempts`
-- rather than adding a row. That keeps the table bounded by distinct
-- deliveries and makes GitHub's retry behaviour visible.

CREATE TABLE IF NOT EXISTS scm_webhook_rejections (
    id             BIGSERIAL PRIMARY KEY,
    scm_provider   TEXT NOT NULL
                       CHECK (scm_provider IN ('github', 'gitlab')),
    scm_base_url   TEXT NOT NULL,
    delivery_id    TEXT NOT NULL
                       CHECK (length(delivery_id) BETWEEN 1 AND 255),
    event_name     TEXT NOT NULL
                       CHECK (length(event_name) BETWEEN 1 AND 100),
    repo_full_name TEXT NOT NULL
                       CHECK (length(repo_full_name) BETWEEN 1 AND 255),
    reason         TEXT NOT NULL
                       CHECK (reason IN ('repository_not_onboarded')),
    attempts       INTEGER NOT NULL DEFAULT 1
                       CHECK (attempts > 0),
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scm_provider, scm_base_url, delivery_id)
);

CREATE INDEX IF NOT EXISTS scm_webhook_rejections_recent_idx
    ON scm_webhook_rejections (last_seen_at DESC);

CREATE INDEX IF NOT EXISTS scm_webhook_rejections_repository_idx
    ON scm_webhook_rejections (repo_full_name, last_seen_at DESC);
