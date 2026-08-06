# Engineering plan

The delivery phases live in [roadmap.md](roadmap.md). This is the shorter,
faster-moving document: the specific defects and structural work in front of us
right now, in dependency order, and the open questions that gate them.

Status is recorded per item so a reader picking this up cold knows what is left.
Delete an item once it has landed and stayed landed for a release.

**Product shape:** Diffuse keeps the review contract and publication on a
self-hosted server; local `diffuse review` may rent a developer agent CLI.
See [agent-runtimes.md](agent-runtimes.md). The server path is not scheduled
for deletion.

## Open questions that gate the work below

These are choices, not engineering tasks. The items further down that depend on
them say so.

- **Capture a review-quality baseline**, and choose the model and depth to
  capture it at. This gates the quality gate itself, honest measurement of
  everything else, and merging any agent-CLI review runtime. It is the cheapest
  one and unblocks the most: everything needed is committed and working, see
  [`../evals/CAPTURE.md`](../evals/CAPTURE.md).
- **Source-execution validation** (executing pull-request code in a sandbox):
  keep, defer, or drop. Distinct from `REVIEW_RUNTIME` and agent CLIs.
- **Learned rules** — who works the approval queue, or whether there is an
  auto-activation path. This gates the "grows with you" claim; see the open
  questions in [roadmap.md](roadmap.md).
- **How much reproducibility to trade for agentic retrieval**, which gates
  giving the reviewer its own tools.
- **Re-cutting the frozen version-1 migration baseline** so the PostgreSQL image
  can drop pgvector. Nothing depends on it urgently; worth settling before the
  next release.
- **The retrieval-eval corpus** — expand the synthetic fixtures into real trees,
  or label real merged pull requests. This gates the retrieval gate.

## Wave 0 — instruments

Nothing after this is verifiable without it.

- **Capture and commit the review baseline; wire `scripts/eval.sh` into CI.**
  *Blocked on capturing the baseline.* Measures the one-shot API review runtime
  — not retrieval, not policy, not an agent CLI. Prove it can fail against a
  seeded regression before
  trusting it (`../evals/CAPTURE.md` §6); a gate that has never failed is not
  yet known to be a gate.
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

## Wave 2 — review runtimes and tools

Diffuse owns the contract (`ReviewReport`, policy, publication). Runtimes are
pluggable; see [agent-runtimes.md](agent-runtimes.md).

- **`ReviewRuntime` seam at `generate_review`.** *Done.* `LiteLLMRuntime` is
  the only selectable implementation (`REVIEW_RUNTIME=litellm`).
- **Agent CLI host plumbing.** *Done for Claude and Codex.* Config dir, sandbox
  policy, version floor (measured for Claude; not yet set for Codex),
  `diffuse agent login|status|write-policy`. Adapters that make either a selectable
  `REVIEW_RUNTIME` are still open.
- **Claude Code adapter.** *Not started.* Blocked on the baseline. Exit: fixture
  review completes, `review_tool_calls` has rows, `claude` enters
  `RUNTIME_NAMES` (not `HOSTED_RUNTIME_NAMES`).
- **`ReviewRequest` + internal tool provider.** *Done (preflight).* Runtimes
  take a `ReviewRequest` (diff, policy, optional worktree / context plan /
  tools). `ReviewToolProvider.search_code` wraps the same `search_codebase`
  MCP uses and records every call (memory or Postgres). Local `diffuse review`
  builds tools onto the request; the one-shot runtime still ignores them.
- **Tool-call log.** *Done (schema).* `review_tool_calls` (migration 0011)
  records every call so an agentic investigation stays replayable. The
  Postgres recorder is ready; the adapter is what must write rows on a real
  review run.
- **Retrievers as tools.** `search_code` and `ask_codebase` are exposed over MCP
  to external agents but unused by Diffuse's own one-shot runtime, which receives
  a pre-fused blob capped at 18 chunks and 24,000 characters. The agent-CLI
  runtime should consume them as tools. *Blocked on the adapter, on the
  reproducibility trade-off, and — for measurement — on the retrieval corpus.*
- **Extend the harness to exercise retrieval.** *Blocked on the retrieval corpus.*
- **Retire one-shot pass scaffolding** (`REVIEW_PASSES` fan-out, diff chunking,
  pre-fused blob, verifier pass) from being the default story — and from the
  agent path — only after measured parity on fixtures. Not before.
- **Codex adapter.** Restores cross-family verification across CLIs. Empirics
  still unmeasured.
- **Record runtime (+ CLI version) on eval runs and review runs.** Partial:
  `ReviewReport` now records the candidate/verifier token split, so the harness
  no longer depends on the one-shot `_call_structured` seam. Runtime and CLI
  version recording remain for the agent adapter.

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
- **Server-owned SCM projection** — pull requests, lifecycle events,
  publications, threads, check runs, webhook deliveries. These stay durable for
  the self-hosted server path (idempotent publication, lineage, analytics).
  Revisit *what* is stored and how much mirrors GitHub; do not erase the
  publication contract because local review exists.

Why derived/decided still matter: under a CLI-in-Actions model every review must
reach the database, and GitHub's published `actions` egress range is 7,297 CIDRs
covering about 27.9 million addresses — not allowlistable, against 6 CIDRs for
`hooks`. "Source never leaves your boundary" and "internet-exposed Postgres
holding the whole index" cannot both be true. Fork pull requests also receive no
secrets, so a pure Actions-CLI model cannot review outside contributions without
`pull_request_target` — another reason the self-hosted worker stays.

**Unsolved inside this wave:** `repository_mirror` lives in `service/hosted/`
with the server surface, but a local-branch CLI still needs something to mirror
repositories. That gap has no answer yet.

## Self-hosted server surface

`service/hosted/` holds the webhook ingress, durable queue, worker, REST/MCP
HTTP app, and related server modules. They are the fleet/fork-PR path and are
**not** obsolete. Local `diffuse review` and agent-CLI runtimes sit beside them.
Edges into this package are listed in `service/hosted/__init__.py`.

What the CLI still must absorb over time (without deleting the server): running
learning inference without only enqueueing a job, and a clear home for
repository mirroring shared by CLI and worker.

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
