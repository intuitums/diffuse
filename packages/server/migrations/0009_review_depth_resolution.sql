-- What the review model was actually asked to do, recorded with the run it
-- applied to. `REVIEW_DEPTH` is resolved per model, and provenance routing
-- permutes the configured pair per pull request, so the resolution that applied
-- to a given review is not derivable after the fact from configuration alone.
-- Nullable: NULL means no depth was requested, which is the documented default.
ALTER TABLE review_runs
    ADD COLUMN review_depth_resolution TEXT;

ALTER TABLE review_runs
    ADD CONSTRAINT review_runs_review_depth_resolution_bounded
        CHECK (
            review_depth_resolution IS NULL
            OR (
                review_depth_resolution <> ''
                AND octet_length(review_depth_resolution) <= 8192
            )
        );
