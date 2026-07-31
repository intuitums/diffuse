# Engineering plan

The delivery phases live in [roadmap.md](roadmap.md). This is the shorter,
faster-moving document: the specific defects and structural work in front of us
right now, in dependency order, and the decisions that block them.

Status is recorded per item so a reader picking this up cold knows what is left.
Delete an item once it has landed and stayed landed for a release.

## Decisions that block work

Each of these is an owner decision, not an engineering task. Nothing below them
can proceed until they are recorded.

| # | Decision | Blocks | Status |
| --- | --- | --- | --- |
| D1 | Fund one baseline capture run, and choose the model and depth it is captured at | The review-quality gate, and therefore honest measurement of everything else | **Open** |
| D2 | Phase 5 (runtime validation): keep, defer, or delete | The roadmap's largest scope item | **Open** |
| D3 | Learned rules: who works the approval queue, or is there an auto-activation path | The "grows with you" thesis | **Open** — see [roadmap.md](roadmap.md) open questions |
| D4 | How much reproducibility to trade for agentic retrieval | Giving the reviewer its own tools | **Open** |
| D5 | Re-cut the frozen version-1 migration baseline, so the Postgres image can drop pgvector | Nothing urgent; decide before the next release | **Open** |
| D6 | The retrieval-eval corpus: expand the synthetic fixtures into real trees, or label real merged pull requests | The retrieval gate | **Open** |

D1 is the cheapest and unblocks the most. Everything needed for it is committed
and working; see [`../evals/CAPTURE.md`](../evals/CAPTURE.md).

## Wave 0 — instruments

Nothing after this is verifiable without it.

- **Capture and commit the review baseline; wire `scripts/eval.sh` into CI.**
  *Blocked on D1.* Measures the review engine only — not retrieval, not policy.
  Prove it can fail against a seeded regression before trusting it
  (`../evals/CAPTURE.md` §6); a gate that has never failed is not yet known to
  be a gate.
- **Make token cost observable.** *Done.* Cache-read and cache-write counts
  accumulate through the review engine. Note that LiteLLM's `prompt_tokens`
  already includes the cached portions, unlike Anthropic's native
  `input_tokens` — summing all three double-counts.

## Wave 1 — the defects

- **Prompt ordering and cache breakpoints.** *Done.* Stable blocks now precede
  the diff chunk, and an Anthropic-only `cache_control` breakpoint covers them.
  Expect four cache entries, not one: the system prompt varies per pass and
  renders before messages. Collapsing to one entry needs a stable system prompt
  with the pass instruction moved into the cached body.
- **Auto-approval: allowlist, not denylist.** *Done.* Eligibility is opt-in per
  path via `.diffuse`, resolved from the indexed default-branch snapshot so a
  pull request cannot grant itself eligibility. The built-in critical-surface
  patterns are a floor an operator allowlist cannot override.

## Wave 2 — give the reviewer its own tools

`search_code` and `ask_codebase` are exposed over MCP to external agents but
unused by Diffuse's own review engine, which receives a pre-fused blob capped at
18 chunks and 24,000 characters and gets one shot at it.

- **Tool-call log.** *Done.* `review_tool_calls` (migration 0011) records every
  call so an agentic investigation stays replayable. Built before the tools
  deliberately — retrofitting it is far harder. Attempts are discriminated by
  `attempt_started_at`, because `begin_review_run` resets a run in place on
  retry through two separate branches.
- **Extend the harness to exercise retrieval.** *Blocked on D6.* The current
  fixtures are single flattened files with no repository tree, so there is
  nothing to retrieve *from*. The labeling schema already exists: each fixture's
  `contexts` block names the file, symbol, and retrieval reason, and a retrieval
  eval would use it as expected output rather than as input.
- **Reviewer consumes the tools.** *Blocked on the above and D4.* Measure recall
  *and* false-positive rate. Precision is the defensible position; do not trade
  it for recall without seeing both numbers.

## Wave 3 — split the state by lifetime

Indexing this repository takes about seven seconds and produces roughly 20 MB
from 3.5 MB of source, with no network and no model credential. That measurement
is what motivates the split.

- **Derived** — snapshots, chunks, symbols, relationships, policy layers.
  Recomputable in seconds. Should become a SQLite file built by `diffuse index`
  and cached on the commit plus `INDEX_FORMAT_VERSION`. The port is cheap now
  that pgvector is gone: what remains Postgres-specific in `indexer/store.py` is
  one advisory lock, four `execute_values`, three `= ANY()`, one `DISTINCT ON`,
  and one `websearch_to_tsquery`/`ts_rank_cd` pair that maps onto FTS5 + bm25.
- **Decided** — learned rules, feedback, lineage, clusters, approvals. Small,
  durable, and not editable by the pull-request author. Postgres stays, reached
  by `diffuse learn` on a schedule and by whoever works the approval queue —
  not by every review.
- **GitHub-owned** — pull requests, lifecycle events, publications, threads,
  check runs, webhook deliveries. Stop storing these.

Why it matters beyond tidiness: under a CLI-in-Actions model every review must
reach the database, and GitHub's published `actions` egress range is 7,297 CIDRs
covering about 27.9 million addresses — not allowlistable, against 6 CIDRs for
`hooks`. "Source never leaves your boundary" and "internet-exposed Postgres
holding the whole index" cannot both be true. Fork pull requests also receive no
secrets, so the CLI model cannot review outside contributions at all without
`pull_request_target`.

**Unsolved inside this wave:** `repository_mirror` is quarantined in
`service/hosted/` as service-tier code, but a local-branch CLI still needs
something to mirror repositories. That gap has no answer yet.

## Deleting the service tier

`service/hosted/` holds the eleven modules — about 9,300 lines — that the CLI
pivot makes obsolete. They are grouped so the deletion is close to
`git rm -r service/hosted`, but it is **not** a cleanup that can be done now:
every one has live importers today. `workflow.py` alone is imported by
`review/engine.py`, `repository_actions.py`, `mcp_actions.py`, and
`cli/repository.py`, and `diffuse learning learn` only enqueues a job for the
worker rather than doing the inference itself.

`service/hosted/__init__.py` enumerates the five surviving edges. The deletion
happens when the CLI absorbs indexing, review triggering, and learning — not
before. `cli/token.py` should go out with it.

## Known gaps this plan does not cover

- No policy evaluation exists in any phase: the harness runs with `policy=None`,
  so no repository rule, path scope, confidence floor, or learned rule is
  exercised by any measurement.
- `tests/integration/` is not self-isolating. A rerun within the same minute can
  fail on state left by the previous run; recreate the database rather than
  debugging it.
- `evals/baseline.example.json` (a hand-written example suite) sits beside
  `evals/baselines/` (captured baselines). Renaming the former would break the
  release image, so the two are disambiguated in prose only.
