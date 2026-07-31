# Capturing the review-quality goldens

**No golden is committed, and the regression gate in `scripts/eval.sh` is
therefore not live.** A golden records what a real model actually found in the
fixtures, so producing one requires live model calls. It cannot be derived,
mocked, or reasoned out — and a fabricated one would be worse than none, because
every later phase of the rebuild diffs against it.

This document is the exact procedure to run once a credential exists.

## 1. What you need

| Requirement | Why |
| --- | --- |
| `REVIEW_MODEL` | The candidate model. There is no default; the run refuses without it. |
| That provider's API key | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, … as `service/model_providers.py` resolves it. |
| `REVIEW_VERIFIER_MODEL` *(optional)* | Defaults to `REVIEW_MODEL`. If you set it to a different model you must also pass its rate card — see step 3. |
| `REVIEW_DEPTH` *(optional)* | Costs real money. `diffuse model` shows what the depth actually becomes on your model before you spend anything. |

The golden records all of these, plus `PROMPT_VERSION`, `MIN_REVIEW_CONFIDENCE`,
`REVIEW_PASSES`, and — per stage — the reasoning parameter each model was
**actually sent**. `check` refuses a comparison across any of them, exactly as it
refuses one across models. You do not have to remember to write them down; you
do have to make sure the shell you capture in holds the values you meant.

`run` resolves the requested depth against both configured models before the
first call and refuses one the candidate cannot express, so a capture cannot
silently record a depth that was never sent.

**No database is needed.** The harness calls `generate_review` directly with
fixture-supplied context, so it does not touch Postgres, the indexer, or the
retriever. That is deliberate: a golden that depended on the state of an index
snapshot would drift for reasons unrelated to review quality.

## 2. Capture

```sh
export REVIEW_MODEL='anthropic/claude-sonnet-5'   # or whatever you hold a key for
export ANTHROPIC_API_KEY='...'                    # that provider's key

python -m service.eval_harness run \
  --fixtures evals/fixtures \
  --output /tmp/suite.json

python -m service.eval_harness capture \
  --suite /tmp/suite.json \
  --golden evals/golden/review-baseline.json
```

`run` is the only command that calls a model. `capture` and `check` are pure
functions of `/tmp/suite.json`, so you can re-score and re-compare as often as
you like without paying again. **Keep `/tmp/suite.json`** — it holds the actual
findings, which the golden deliberately does not.

`--output` is rewritten after every fixture, so if a provider errors partway
through, the completed cases are already on disk. Resume with:

```sh
python -m service.eval_harness run \
  --fixtures evals/fixtures \
  --output /tmp/suite.json \
  --resume
```

Only the fixtures that have not completed are re-reviewed, and a fixture edited
since the first attempt is re-run rather than reused.

## 3. Pricing, if you want a cost figure

`estimated_cost_usd` is 0 unless you supply rates. Nothing guesses them:

```sh
python -m service.eval_harness run \
  --fixtures evals/fixtures --output /tmp/suite.json \
  --input-usd-per-million 3 --output-usd-per-million 15
```

If `REVIEW_VERIFIER_MODEL` differs from `REVIEW_MODEL`, the two bill at
different rates and `service/evaluation.py` refuses a suite that prices them the
same. The harness checks this **before** the first model call, so the refusal
costs nothing:

```sh
  --verifier-input-usd-per-million 0.8 --verifier-output-usd-per-million 4
```

## 4. Expected cost and duration

Measured from the committed fixtures, not estimated from memory. Every fixture
packs into a single diff chunk, and none is large enough to trigger the diagram
stage, so a full capture is:

- 8 fixtures x (4 candidate passes + 1 verification) = **40 model calls**
- **≈ 50,000 input tokens** total (measured: ~46k for the candidate prompts plus
  the verification prompts)
- **≈ 40,000 output tokens** with no depth requested. With
  `REVIEW_DEPTH=thorough`,
  reasoning tokens bill as output and this is the term that explodes — budget
  **250,000–600,000 output tokens**.

Worked at an example **$3 / $15 per million** rate card — *confirm your
provider's current rates, do not trust this number*:

| Setting | Cost per capture | Wall clock (serial) |
| --- | --- | --- |
| no depth requested | **≈ $0.75** | 7–20 min |
| `REVIEW_DEPTH=thorough` | **≈ $4–5** | 40–80 min |

On a small model (an example $0.40 / $1.60 rate card) the same run is under
**$0.10**. Capturing on a cheap model first to shake out the plumbing, then
recapturing on the model you actually ship, is the sensible order. Note that
`openai/gpt-4.1-mini` has no reasoning control at all, so `run` will refuse a
`REVIEW_DEPTH` on it rather than capture a golden at a depth it never sent.

The harness reviews fixtures serially. There is no parallelism, on purpose:
concurrency would make the recorded `latency_ms` meaningless.

## 5. Verify the golden before committing it

A golden is the standard every later phase defends. Committing a bad one locks
in whatever it recorded. Check all of these:

1. **Read the scores.** `capture` prints precision, recall and F1. Recall near
   0 does not mean the fixtures are wrong; it means the review engine did not
   find bugs that are unambiguously present, and that is a finding about the
   product, not a reason to weaken the labels. **Do not adjust a fixture to make
   the numbers look better.** Report the number. A recall of *exactly* zero is
   refused outright — such a golden passes against every later run, including
   one where the engine returns nothing — and needs `--allow-zero-recall` to
   record deliberately.
2. **Read the findings**, in `/tmp/suite.json`, not just the scores. For each
   fixture, is the observed finding the labeled bug, or a different issue that
   happens to sit within the line tolerance? A coincidental match inflates
   recall and is invisible in the score.
3. **Check `clean-settings-refactor`.** It has no labeled defect. Every finding
   it produces is a false positive. If it produces several, the confidence
   threshold is too low, and that is exactly the constant this harness exists to
   measure.
4. **Read `category_mismatches` and `category_confusion`.** Category is not part
   of the match, so a real detection filed under `reliability` where the label
   says `correctness` is one true positive and one entry in this table — it does
   not move precision or recall. Read the table anyway: a repeated confusion in
   one direction often means the label is the wrong one, and fixing a label
   after capture invalidates the golden, so decide before you commit it.

   Two caveats before you change a label on the strength of it. The table is
   order-independent but **not minimal** — the matcher prefers a
   category-agreeing observation without solving the assignment optimally, so a
   single entry can be an artifact of which maximum matching was chosen. And a
   single entry on a single run is one model's opinion. Change a label only for
   a confusion that repeats.
5. **Read `severity_mismatches` and `severity_confusion`.** These are *not*
   free diagnostics: every entry is a labeled defect the reviewer located and
   graded differently, charged as a false negative **and** a false positive. If
   this table is not empty, the question is whether the grade was really part of
   the claim — a label without a severity places no such constraint. Decide
   before capture; changing a label afterwards invalidates the golden.
6. **Run it twice.** These models are not deterministic. If two consecutive
   captures disagree by more than a few points, commit the *worse* run as the
   golden and set a non-zero `EVAL_TOLERANCE`, rather than committing a lucky
   run that every later change appears to regress against.
7. **Record the configuration** in the commit message: model, verifier model,
   `REVIEW_DEPTH`, `MIN_REVIEW_CONFIDENCE`, `REVIEW_PASSES`, and the date. The
   golden now stores all of these and refuses a comparison across any of them,
   so this is for the reader rather than for the gate — but the date and the
   provider's model revision are still yours to record.

Then commit `evals/golden/review-baseline.json` and confirm the gate is live:

```sh
./scripts/eval.sh                      # runs, scores, compares. Exit 0 = no regression.
EVAL_SUITE=/tmp/suite.json ./scripts/eval.sh   # re-compare without paying again
```

## 6. Prove the gate actually catches something

Do not take a green run as evidence that the gate works. Seed a regression and
watch it fail — the plan's acceptance criterion for this unit:

```sh
# In service/review_engine.py, raise the effective confidence floor at the
# point it is applied, inside the candidate filter (~line 1048):
#   min(candidate.confidence, decision.confidence) < threshold
#   ->
#   min(candidate.confidence, decision.confidence) < threshold + 0.2
python -m service.eval_harness run --fixtures evals/fixtures --output /tmp/regressed.json
EVAL_SUITE=/tmp/regressed.json ./scripts/eval.sh    # must exit 1
```

**Seed it there, not in `minimum_review_confidence()`.** That function's value is
recorded in the golden's `run_configuration`, so changing it makes `check` exit 1
on a configuration mismatch — which is correct behaviour and proves nothing about
review quality. The seed has to move the findings while leaving the recorded
configuration alone, and then the failure you read must be a per-case miss and a
recall drop.

Revert the change afterwards. Until this has been done once, the gate is
unproven even with a golden committed.
