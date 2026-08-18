-- DEV-313 makes a check whose provider PATCH failed reclaimable by the stranded
-- sweep, so that one transient 5xx no longer leaves a required check
-- `in_progress` on GitHub forever. That reclaim needs a bound, and the existing
-- `attempt_count` cannot supply one: `begin_check_run` increments it, so it
-- counts how many times a check was *created*, not how many times Diffuse tried
-- to terminalize it.
--
-- Without a separate bound the sweep has no exit at all. A check that can never
-- be completed -- the App was uninstalled, the check run was deleted, the repo
-- is gone -- keeps satisfying the reclaim predicate on every pass. It is also
-- the oldest such row, and `claim_stranded_review_jobs` orders by
-- `completed_at` and takes twenty, so a handful of them starve every newer
-- stranded job behind them.
--
-- Counting completion attempts separately gives the sweep a terminal state for
-- the unrecoverable case while leaving the transient one retried.

ALTER TABLE review_check_runs
    ADD COLUMN IF NOT EXISTS completion_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (completion_attempts >= 0);

-- The reclaim predicate filters on status and this counter together.
CREATE INDEX IF NOT EXISTS review_check_runs_completion_attempts_idx
    ON review_check_runs (status, completion_attempts);
