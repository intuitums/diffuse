-- Prompt-cache counters are a breakdown of review_runs.prompt_tokens, not an
-- addition to it. The review engine already fills them on ReviewReport when the
-- Anthropic route reports cache_read_input_tokens / cache_creation_input_tokens;
-- without columns they were discarded at complete_review_run and every reloaded
-- report read back as zero. Cache reads bill at about a tenth of the base input
-- rate and writes at 1.25x, so prompt_tokens alone cannot say whether a review
-- was cheap or full price.
--
-- No CHECK tying the sum to prompt_tokens: LiteLLM's folding of the two cache
-- counters into prompt_tokens is the contract the engine documents, but a
-- provider quirk that briefly disagrees must not abort persisting a finished
-- review.

ALTER TABLE review_runs
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (cache_read_tokens >= 0),
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (cache_write_tokens >= 0);
