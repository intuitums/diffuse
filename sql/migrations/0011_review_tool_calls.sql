-- Every tool call a review run makes, in the order it made them.
--
-- ADR 0001 buys reproducibility by pinning what a review read: snapshots are
-- immutable and commit-pinned, and `review_run_context_snapshots` records the
-- exact ones a run used. That is sufficient only while retrieval is a single
-- pre-fused blob computed from the diff -- same snapshots plus same diff yields
-- the same context. An agentic reviewer breaks that: it chooses its own queries,
-- the choice is nondeterministic, and the snapshot set no longer determines what
-- the model saw. The ordered call log is what keeps "this review is reproducible,
-- and here is exactly what the model saw" a true statement.
--
-- This lands before the reviewer can call a tool. There is no way to reconstruct
-- an investigation after the fact, so a log added later would leave a permanent
-- band of reviews that cannot be replayed at all -- and retrofitting the write
-- path through a reviewer that already ships is strictly harder than having the
-- table waiting for it.

CREATE TABLE IF NOT EXISTS review_tool_calls (
    id                       BIGSERIAL PRIMARY KEY,
    review_run_id            BIGINT NOT NULL
                                 REFERENCES review_runs (id) ON DELETE CASCADE,
    -- Which attempt of the run made this call. A review run is *reused* across
    -- retries: `begin_review_run` resets a run that is `failed` or `generating`
    -- in place, keeping the same id and refreshing `started_at`. The other
    -- run-scoped children answer that by deleting and re-inserting; an
    -- append-only log cannot, and should not want to -- what the reviewer read
    -- before it failed is precisely the evidence this table exists to keep. So
    -- the attempt is part of a call's identity, and `review_runs.started_at` is
    -- what names it: both reset branches refresh it, it needs no new column on
    -- `review_runs`, and being a clock it also orders the attempts against each
    -- other. Rows carrying the run's current `started_at` are the attempt that
    -- produced the review that shipped; every earlier value is a retained one.
    --
    -- Two attempts collapse into one only if a reset shares a transaction with
    -- the attempt it replaces, since `now()` is fixed for a transaction. That
    -- degrades to one continuous ordinal sequence -- the behaviour before this
    -- column existed -- rather than to a collision or a lost row.
    attempt_started_at       TIMESTAMPTZ NOT NULL,
    -- The step number of the investigation, allocated per attempt. `id` comes
    -- from a table-wide sequence that gaps on rollback and interleaves with
    -- concurrent runs, so it can order rows but cannot name a step; a replay
    -- that reports "call 4 diverged" has to mean the same call the original
    -- numbered 4. UNIQUE below is therefore also the append-only guard:
    -- re-recording a step fails instead of silently producing two histories.
    ordinal                  INTEGER NOT NULL CHECK (ordinal > 0),
    -- Bounded because the name is chosen by the model's tool call, not by us. An
    -- unbounded TEXT here lets one malformed turn write a megabyte into the
    -- column every audit query groups by.
    tool_name                TEXT NOT NULL
                                 CHECK (tool_name ~ '^[a-z][a-z0-9_]{0,63}$'),
    -- The request exactly as issued. Kept structured rather than rendered so a
    -- replay can re-issue it without reparsing a string the model wrote.
    arguments                JSONB NOT NULL
                                 CHECK (jsonb_typeof(arguments) = 'object'),
    arguments_truncated      BOOLEAN NOT NULL DEFAULT FALSE,
    -- Digests are taken over the canonical JSON *before* it reaches PostgreSQL.
    -- Recomputing them from the stored row is not equivalent: jsonb orders keys
    -- by its own rule and discards duplicate keys, so the bytes that came back
    -- from the tool are not recoverable from the value. A digest of the
    -- untruncated payload is what lets a replay prove equality even when the
    -- copy retained here is partial.
    arguments_sha256         TEXT NOT NULL
                                 CHECK (arguments_sha256 ~ '^[0-9a-f]{64}$'),
    arguments_bytes          INTEGER NOT NULL CHECK (arguments_bytes > 0),
    -- A tool call that raised is evidence about the investigation, not an error
    -- to drop: a review that reached its conclusion after three failed lookups
    -- reasoned from a different world than one whose lookups all answered.
    status                   TEXT NOT NULL
                                 CHECK (status IN ('succeeded', 'failed')),
    -- NULL only when the call produced nothing at all. A failed call that still
    -- returned a partial body keeps it.
    result                   JSONB
                                 CHECK (
                                     result IS NULL
                                     OR jsonb_typeof(result) = 'object'
                                 ),
    result_truncated         BOOLEAN NOT NULL DEFAULT FALSE,
    result_sha256            TEXT
                                 CHECK (
                                     result_sha256 IS NULL
                                     OR result_sha256 ~ '^[0-9a-f]{64}$'
                                 ),
    result_bytes             INTEGER CHECK (result_bytes IS NULL OR result_bytes >= 0),
    failure_code             TEXT
                                 CHECK (
                                     failure_code IS NULL
                                     OR failure_code ~ '^[a-z0-9_]{1,64}$'
                                 ),
    failure_detail           TEXT
                                 CHECK (
                                     failure_detail IS NULL
                                     OR length(failure_detail) BETWEEN 1 AND 2000
                                 ),
    -- The snapshots this one call actually read. The run-level pinning in
    -- `review_run_context_snapshots` is the set the run was *allowed* to read;
    -- which of them a given query touched is per call, and a replay against the
    -- wrong ones is not a replay. Deliberately no foreign key, matching
    -- `review_run_learned_rules`: deleting a snapshot must not erase the record
    -- of a review that used it.
    index_snapshot_ids       BIGINT[] NOT NULL DEFAULT '{}',
    -- `service/code_query.py` already fingerprints the cross-repository plan it
    -- resolved. Storing it makes "the index moved under us" a one-column
    -- comparison instead of an array diff.
    context_plan_fingerprint TEXT
                                 CHECK (
                                     context_plan_fingerprint IS NULL
                                     OR context_plan_fingerprint ~ '^[0-9a-f]{64}$'
                                 ),
    -- Wall-clock cost of the call itself, separate from when it was written.
    -- Recording only `recorded_at` would fold queue and retry time into the
    -- measurement and make a slow tool indistinguishable from a slow reviewer.
    duration_ms              INTEGER NOT NULL CHECK (duration_ms >= 0),
    started_at               TIMESTAMPTZ NOT NULL,
    recorded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Both the append-only guard and the index for the only read path there is
    -- (one run's calls, attempt by attempt, each in order). A separate index
    -- would duplicate it. The attempt sits inside the key because a retry
    -- restarts the step numbering at 1: keyed on the run alone, the retry's
    -- first call is rejected as a duplicate of the first attempt's step 1, so
    -- the retried review loses its log entirely rather than merely being
    -- ambiguous about which attempt each step belongs to.
    UNIQUE (review_run_id, attempt_started_at, ordinal),
    CHECK (
        (status = 'succeeded' AND failure_code IS NULL AND result IS NOT NULL)
        OR (status = 'failed' AND failure_code IS NOT NULL)
    ),
    -- A payload without its digest and size is unreplayable, and a digest
    -- without its payload describes nothing that is here.
    CHECK ((result IS NULL) = (result_sha256 IS NULL)),
    CHECK ((result IS NULL) = (result_bytes IS NULL)),
    CHECK (result IS NOT NULL OR NOT result_truncated),
    -- The storage bound, enforced again here so a caller that bypasses
    -- `service/review/tool_log.py` cannot put a multi-megabyte `search_code`
    -- body in a row. The writer's budget is 8 KiB of arguments and 64 KiB of
    -- result measured as compact canonical JSON; these limits are the doubled
    -- backstop, because jsonb renders its own text with whitespace the writer's
    -- accounting does not include and an exact bound here would reject payloads
    -- the policy accepted.
    CHECK (octet_length(arguments::TEXT) <= 16384),
    CHECK (result IS NULL OR octet_length(result::TEXT) <= 131072),
    -- One call reads the primary snapshot plus at most the six related ones
    -- `review_run_context_snapshots` allows. The generous ceiling only stops the
    -- array from becoming the unbounded payload the two limits above prevent.
    CHECK (
        array_length(index_snapshot_ids, 1) IS NULL
        OR array_length(index_snapshot_ids, 1) <= 64
    )
);
