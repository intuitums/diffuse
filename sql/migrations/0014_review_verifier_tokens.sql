-- The candidate and verifier stages can run on models from different families,
-- and a cross-family pair does not share a rate card. `ReviewReport` now carries
-- the split, but `persist_review_report` writes an explicit column list, so
-- without these columns the split is dropped on write and every reloaded report
-- reads back as "the verifier cost nothing" -- which is a cost figure that looks
-- authoritative and is wrong. The eval harness kept the numbers only because it
-- scores the in-memory report and never reloads one.
--
-- Same shape as the cache counters in 0012: a breakdown of prompt_tokens and
-- completion_tokens, never an addition to them.
--
-- Nullable, and deliberately without a default. NULL means "this runtime did
-- not report a split", which is exactly true of every row written before this
-- migration; backfilling those to 0 would assert the verifier was free. A
-- runtime that really ran no verification stage writes 0 and means it.
--
-- No CHECK tying these to the totals. `ReviewReport` already rejects a verifier
-- count larger than its total at the model boundary, and a provider quirk that
-- briefly disagrees must not abort persisting a finished review.

ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS verifier_prompt_tokens INTEGER
        CHECK (verifier_prompt_tokens IS NULL OR verifier_prompt_tokens >= 0),
    ADD COLUMN IF NOT EXISTS verifier_completion_tokens INTEGER
        CHECK (verifier_completion_tokens IS NULL OR verifier_completion_tokens >= 0);
