# Diffuse review evaluation

`diffuse evaluate` scores labeled findings against a model run. The matching
unit is a category plus file and line location (within the label's explicit
tolerance); an optional severity label makes severity part of the match.

Each case records expected bugs, observed findings, developer-addressed label
IDs, latency, and token usage. The resulting JSON reports precision, recall,
F1, false positives, false negatives, addressed findings, median latency, and
estimated model cost.

Candidate and verification tokens are counted and priced separately, because a
cross-family pair does not share a rate card. `prompt_tokens` and
`completion_tokens` are the candidate stage; add `verifier_prompt_tokens` and
`verifier_completion_tokens` per case, plus a suite-level `verifier_model` and
`verifier_pricing`, whenever verification ran on a differently priced model. A
suite that records verifier tokens without saying what they cost is rejected
rather than mispriced.

Thresholds must be finite values in `[0, 1]`. `--min-f1 nan` is refused rather
than accepted as a gate that can never fail.

This scores a run; it does not perform one. There is no harness that invokes
the review engine against fixtures, so `observed` has to be transcribed by hand
from a Diffuse run. Building that harness is open Phase 0 work — see
`docs/roadmap.md`.

Start by copying `baseline.example.json`, replacing the illustrative cases with
real reviewed pull requests, and filling `observed` from a Diffuse run:

```sh
diffuse evaluate evals/baseline.example.json
```

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
