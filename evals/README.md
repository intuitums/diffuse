# Diffuse review evaluation

`diffuse evaluate` scores labeled findings against a model run.

The matching unit is a defect identity: the same file and diff `side`, a line
within the label's own `line_tolerance`, and at least one normalized,
non-generic title token in common. Matching is case-insensitive and punctuation
is a separator, so `SQL-injection` overlaps `sql injection`; generic words such
as `finding`, `issue`, and `the` provide no credit. An optional severity label
makes severity part of the match. **Category is not part of the match** — it is
reported separately, as `category_mismatches` and a `category_confusion` table.

That split is deliberate. `category` is mandatory on a label, so requiring
equality gave a labeller no way to opt out of it, and a model that found a real
SQL injection and filed it under `correctness` scored as *both* a false
negative and a false positive: strictly worse than a model that missed the bug
entirely, which pays only the false negative. Category disagreement is a real
signal — a reviewer that consistently miscategorises injections is telling you
something about its prompt — but it is a signal about taxonomy, not about
whether the bug was found, so it is reported beside precision and recall rather
than folded into them.

**`severity` keeps gating, and it is not free.** A label opts into severity by
stating one, and leaving it unset is the default — that opt-out is the whole
reason for the asymmetry with category. But when a label *does* state a
severity and the reviewer grades the defect differently, the label is charged as
a false negative **and** the observation that located it as a false positive:
exactly the double charge that was removed from category. That is deliberate
policy — stating `critical` is a claim that a review calling the defect `high`
has not really found it — but it is expensive, and in the totals alone it is
indistinguishable from a defect nobody noticed plus an unrelated finding.
`severity_mismatches` and the `severity_confusion` table name each one so the
cost is visible. **Do not state a severity on a label unless the grade is part
of the claim you are making.**

`category_mismatches` is a *preference*, not a minimum. The matcher prefers an
observation that agrees on category when a label could be satisfied either way,
but minimising mismatches across all maximum matchings is a min-cost assignment
problem it does not solve, so the reported table can be larger than necessary.
It is order-independent — the same inputs give the same table whatever order the
observations arrive in — but read it as a signal, not as an exact count.

`line_tolerance` defaults to 3, is capped at 10, and **every committed fixture
uses 1**. Title overlap prevents a wholly unrelated finding inside that span
from being credited, but a wide span still increases the surface for a vaguely
related title to collide. Every label here sits exactly on a changed line, so
none of them needs more slack than one line, and the tightest honest span is the
right one.

Matching is one-to-one in both directions. One observation can never satisfy two
labels, and one label can never absorb two observations — a second finding
inside the same span is a false positive, whatever its category. Loosening the
match key cannot, therefore, inflate true positives past the number of distinct
findings the reviewer actually reported.

Each case records expected bugs, observed findings, developer-addressed label
IDs, latency, and token usage. The resulting JSON reports precision, recall,
F1, false positives, false negatives, addressed findings, category mismatches,
severity mismatches, median latency, and estimated model cost.

The suite schema is `diffuse-evaluation-v3` and the score schema is
`diffuse-evaluation-score-v5`. The input version moved because expected and
observed findings now require `title` and `side`; a v2 suite never recorded
either signal and cannot be scored honestly under this gate. A suite the
harness produced also carries a `run_configuration` block; one written by hand
from a reviewed pull request has no run to describe and omits it.

Candidate and verification tokens are counted and priced separately, because a
cross-family pair does not share a rate card. `prompt_tokens` and
`completion_tokens` are the candidate stage; add `verifier_prompt_tokens` and
`verifier_completion_tokens` per case, plus a suite-level `verifier_model` and
`verifier_pricing`, whenever verification ran on a differently priced model. A
suite that records verifier tokens without saying what they cost is rejected
rather than mispriced.

Thresholds must be finite values in `[0, 1]`. `--min-f1 nan` is refused rather
than accepted as a gate that can never fail.

`diffuse evaluate` scores a run; it does not perform one. `service/eval_harness.py`
is what performs one — see **The fixture harness** below. Everything above
describes the scorer, which is unchanged.

To score a suite you already have — one the harness produced, or one you wrote
by hand from a reviewed pull request:

```sh
diffuse evaluate evals/baseline.example.json
```

Despite the name, `baseline.example.json` is not a captured baseline: it is a
hand-written example of the *suite* format `diffuse evaluate` reads. The
captured baseline the fixture harness compares against lives at
`baselines/review-baseline.json` and is described under **What a baseline
records** below.

The shipped fixture records one expected finding and zero observed, so it
scores 0% recall and exits non-zero under any recall gate. That is intentional
— the thresholds below are an example of the syntax, not a passing invocation:

```sh
# Exits 1 against the shipped fixture, by design.
diffuse evaluate evals/baseline.example.json \
  --min-precision 0.80 \
  --min-recall 0.60
```

Keep sensitive diffs and source out of this directory. Labels may reference
private repository paths, but the committed example is synthetic.

---

# The fixture harness

`service/eval_harness.py` runs the real review engine over the fixtures in
`fixtures/` and emits exactly the `EvaluationSuite` above — the `observed` half
included. Nothing is transcribed by hand any more.

```sh
python -m service.eval_harness run --fixtures evals/fixtures --output /tmp/suite.json
python -m service.eval_harness run --output /tmp/suite.json --resume   # after a failure
python -m service.eval_harness capture --suite /tmp/suite.json   # write a baseline
python -m service.eval_harness check   --suite /tmp/suite.json   # exit 1 on a regression
./scripts/eval.sh                                                # run + check
```

Only `run` calls a model. `capture` and `check` are pure functions of a suite
file.

`run` rewrites `--output` after every fixture rather than only at the end, so a
provider error partway through leaves the completed cases on disk and `--resume`
picks up from them. A full capture is 40 serial model calls; losing the seventh
of eight used to discard the other 35. A case whose fixture changed since is
re-run rather than reused.

> **Baselines are not committed and the regression gate is not live.** Capturing
> one requires live model calls. `scripts/eval.sh` exits non-zero with
> instructions rather than passing vacuously. See [CAPTURE.md](CAPTURE.md).

## The fixture format

One directory per case, under `fixtures/`. The directory name *is* the case id,
so a baseline entry cannot drift away from the fixture it scores.

```
fixtures/<case-id>/
  case.json        labels and metadata
  diff.patch       a real unified diff, produced by git
  context/*.py     retrieved context, read verbatim
```

`case.json`:

| Field | Meaning |
| --- | --- |
| `schema_version` | `diffuse-eval-fixture-v2` |
| `case_id` | must equal the directory name |
| `description` | what the defect is, and why it is a defect |
| `diff_path` | default `diff.patch`; must stay inside the fixture |
| `expected` | `ExpectedFinding` records, including a semantic `title` and exact diff `side` |
| `addressed_finding_ids` | labels a developer subsequently fixed |
| `contexts` | retrieved-context entries, each pointing at a file in the fixture |

Three deliberate choices:

**The diff is a file, not a JSON string.** A diff embedded in JSON is escaped
onto one line: unreviewable in a pull request, and impossible to regenerate with
`git diff`. Every committed patch here was produced by `git diff` against a real
tree, so the line numbers are real line numbers.

**Context content is a file too**, and its line span is derived from the file
rather than declared. A fixture cannot claim a context spans lines it does not
have.

**`expected` reuses `ExpectedFinding` verbatim** instead of defining a parallel
label type. The harness feeds the scorer that already exists; a second schema
would be a second thing to keep in step.

## Why these fixtures, and why they are better than `baseline.example.json`

The committed example labels `service/webhook.py:42` and ships **no diff at
all**. `service/review/engine.py` drops any candidate that does not land on a
changed line, so that label is unmatchable by construction — which is why its
recorded run scores 0% recall. It measures nothing.

Every fixture here:

- **is a real diff of real code.** Both sides compile as Python and the defect
  is a defect in the code, not in a comment describing one.
- **puts the defect on a changed line.** `load_fixtures` refuses a label that is
  not within its own `line_tolerance` of an added or deleted line, and a test
  pushes every label through `generate_review` itself to confirm the engine can
  emit it. A fixture cannot silently be unwinnable.
- **carries genuine retrieved context** — the callee whose return type is
  nullable, the route handler that shows the input is attacker-controlled. Where
  a fixture needs context to be judged correctly, that context is supplied, so
  the harness measures the review engine rather than the absence of an index.
- **includes a negative control.** `clean-settings-refactor` has no defect and
  no labels. Without it, precision is unmeasurable, and the whole harness could
  be gamed by lowering the confidence threshold until recall hit 1.0.
- **labels every defect its diff introduces.** `unhandled-error-path` and
  `missing-null-check` each remove two behaviours, so each carries two labels.
  Matching is injective: with one label, a reviewer that correctly reports both
  defects is charged a false positive for the second, which raises that case's
  false-positive floor permanently and weakens the precision half of the gate.
  For the same reason `path-traversal-attachment` introduces exactly one defect
  — an earlier revision also swapped `Path` for `os.path.join` without importing
  `os`, which bought no extra signal and charged a reviewer that correctly
  flagged the missing import.

Coverage: off-by-one, missing null check plus its unguarded caller, an unhandled
error path plus a swallowed rollback, SQL injection, path traversal, a
check-then-act race, an N+1 query, and the clean control. Eight fixtures, nine
labels, and the count is pinned by a test — a directory without a `case.json` is
an error rather than a silent skip.

## What a baseline records

Scores, not prose — plus every condition those scores are only a standard
under. Schema `diffuse-eval-baseline-v3`; v2 baselines used the location-only
matcher and must be recaptured rather than silently reinterpreted.

| Field | |
| --- | --- |
| `suite_name` | |
| `model`, `verifier_model` | the pair the scores were produced by |
| `run_configuration.prompt_version` | `service.review.engine.PROMPT_VERSION` |
| `run_configuration.min_review_confidence` | `MIN_REVIEW_CONFIDENCE` |
| `run_configuration.review_passes` | `REVIEW_PASSES` (API one-shot runtime) |
| `run_configuration.review_runtime` | `REVIEW_RUNTIME` (defaults to `litellm` on older suites) |
| `run_configuration.requested_review_depth` | `REVIEW_DEPTH`, as an intent |
| `run_configuration.depth_renderings` | per stage: what the model was **actually sent** |
| `precision`, `recall`, `f1` | |
| `category_mismatches` | recorded and reported as a delta, never gated |
| `cases[].expected_finding_count` | the label count at capture |
| `cases[].fixture_digest` | SHA-256 of `diff.patch` + `case.json` |
| `cases[].true_positives`, `false_positives`, `false_negatives` | |

Model output is not deterministic, so a byte comparison of titles and summaries
would fail for reasons that have nothing to do with review quality — and a check
that cries wolf gets deleted. Comparing through the scorer catches the thing a
threshold change in `service/review/engine.py` actually moves.

The configuration fields exist because the model name was previously the only
thing pinned, and it is not the only thing that moves the score. Capture with
`MIN_REVIEW_CONFIDENCE=0.99` left in a shell and the baseline records near-zero
recall and near-zero false positives that every later run at the default 0.75
clears trivially, forever — with the precision guard pinned to a floor nobody
chose. `service/cli/review.py` already treats a `PROMPT_VERSION` change as invalidating
a stored run; a baseline outlives many more of them.

`depth_renderings` records the resolved depth, not just the requested one,
because a route can be sent nothing at all whatever the request said.
`openai/gpt-4.1-mini` — the model this document recommends capturing on first —
has no reasoning control, so a run at `REVIEW_DEPTH=thorough` there sends no
reasoning parameter. Recording only the request would let that defend a baseline
captured on a model that honoured it.

`check` fails when any case gets worse (more misses, or more unlabeled
findings), when an aggregate metric drops by more than `--tolerance`, when a
baseline case did not run, when a fixture has no baseline entry, when a
fixture's label set changed since capture, when a fixture's **content** changed
since capture, when the baseline was captured against a different model, or
when any `run_configuration` field differs.

The last three matter for the same reason: editing a fixture or a variable after
capture silently rebases the comparison. Drop a label the engine kept missing
and recall "improves" without the engine changing at all; make the bug more
obvious in `diff.patch` and it improves without the labels changing at all, and
the gate then measures an easier task than the one it was calibrated on.

`check` does **not** fail on `category_mismatches`. The delta is printed, and
`severity_mismatches` is called out separately, because that one is already
inside precision and recall — twice.

`capture` refuses a suite that found none of its labeled defects unless
`--allow-zero-recall` is passed. Such a baseline is structurally valid and
passes against every later run, including one where the review engine returns
nothing: `precision` is 1.0 when there is nothing to be precise about. A real
zero is a finding about the review engine and worth recording deliberately; it
must not happen by accident.

## Known limits

Stated plainly so nobody mistakes a green run for more than it is.

- **No baseline, so no gate.** Everything above is machinery. It has never been
  run against a live model. See [CAPTURE.md](CAPTURE.md).
- **No repository policy is applied.** The harness passes `policy=None`, so the
  engine uses the environment-level `MIN_REVIEW_CONFIDENCE` and `REVIEW_PASSES`.
  Per-path confidence thresholds, severity floors, `summary_only`, and the
  preventative-security rules in `repository_policy/` are *not* exercised. A
  fixture-level policy is the obvious next extension.
- **Title overlap is lexical, not semantic.** Requiring one normalized,
  non-generic token prevents a wholly unrelated nearby finding from matching,
  but a shared domain word can still connect different defects and a pure
  paraphrase with no shared token will miss. **Read the findings, not just the
  score.** The deterministic guard is intentionally auditable; it is not a
  substitute for human review of a capture.
- **`category_mismatches` is a preference, not a minimum.** The matcher prefers
  a category-agreeing observation but does not solve the min-cost assignment,
  so the table can name a confusion a better assignment would have avoided. It
  is stable under reordering of the observed list, and it is recorded in the
  baseline and reported as a delta — never gated.
- **The candidate/verifier token split is observed, not reported.**
  `ReviewReport` carries one combined token pair, so the harness wraps
  `_call_structured` to attribute each call to its stage. The wrapper delegates
  to the real function and changes nothing about the call; it is a workaround
  for a missing field, and the tidier fix is for `generate_review` to return the
  split.
- **The fixtures are synthetic.** Real, self-contained code with real defects,
  but written for this purpose. Reviewed private PRs remain the goal; these
  exist so the harness has something honest to measure in the meantime.
- **`latency_ms` is wall clock**, including provider queueing and any LiteLLM
  retry. It is a rough operational number, not a benchmark.
