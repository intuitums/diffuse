# Diffuse review evaluation

`diffuse evaluate` scores labeled findings against a model run. The matching
unit is a category plus file and line location (within the label's explicit
tolerance); an optional severity label makes severity part of the match.

Each case records expected bugs, observed findings, developer-addressed label
IDs, latency, and token usage. The resulting JSON reports precision, recall,
F1, false positives, false negatives, addressed findings, median latency, and
estimated model cost.

Start by copying `baseline.example.json`, replacing the illustrative cases
with real reviewed pull requests, and filling `observed` from a Diffuse run:

```sh
diffuse evaluate evals/baseline.example.json \
  --min-precision 0.80 \
  --min-recall 0.60
```

Keep sensitive diffs and source out of this directory. Labels may reference
private repository paths, but the committed example is synthetic.
