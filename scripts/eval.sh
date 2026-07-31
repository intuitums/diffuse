#!/usr/bin/env bash
#
# Review-quality regression gate.
#
#   1. run the real review engine over every fixture in evals/fixtures/
#   2. score the result with service/evaluation.py
#   3. compare against the committed golden, and exit non-zero on a regression
#
# THE GATE IS NOT LIVE YET. No golden is committed, because capturing one needs
# live model calls and therefore a real credential and a real spend. Until
# evals/golden/review-baseline.json exists this script exits non-zero with
# instructions rather than passing, which is the honest state: a regression
# check that goes green because it has nothing to compare against is worse than
# no check at all. See evals/CAPTURE.md.
#
# Step 1 is the only step that costs money, so the golden is checked for first.
#
# Environment:
#   REVIEW_MODEL           required by the review engine; there is no default
#   REVIEW_VERIFIER_MODEL  optional; defaults to REVIEW_MODEL
#   EVAL_FIXTURES          fixture directory        (default: evals/fixtures)
#   EVAL_GOLDEN            golden file              (default: evals/golden/review-baseline.json)
#   EVAL_SUITE             score this suite instead of running one (no model calls)
#   EVAL_SUITE_OUT         where to write the run's suite JSON (default: a temp file)
#   EVAL_TOLERANCE         allowed absolute drop in precision/recall/F1 (default: 0)
#   EVAL_PRICING_ARGS      extra `run` flags, e.g. --input-usd-per-million 3
#
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
FIXTURES="${EVAL_FIXTURES:-evals/fixtures}"
GOLDEN="${EVAL_GOLDEN:-evals/golden/review-baseline.json}"
TOLERANCE="${EVAL_TOLERANCE:-0}"

if [ ! -f "$GOLDEN" ]; then
  cat >&2 <<EOF
error: no golden at $GOLDEN.

The review-quality regression gate is not live. A golden records the scores of
a real review run, so it cannot be generated offline and none is committed.

To capture one, follow evals/CAPTURE.md. In short:

  export REVIEW_MODEL=<litellm model id>
  export <that provider's API key>
  $PYTHON -m service.eval_harness run --fixtures $FIXTURES --output /tmp/suite.json
  $PYTHON -m service.eval_harness capture --suite /tmp/suite.json --golden $GOLDEN

Review the captured numbers before committing them; a golden with 0% recall
locks in a broken review engine as the standard to defend.
EOF
  exit 1
fi

if [ -n "${EVAL_SUITE:-}" ]; then
  SUITE="$EVAL_SUITE"
  echo "scoring existing suite $SUITE (no model calls)" >&2
else
  if [ -n "${EVAL_SUITE_OUT:-}" ]; then
    SUITE="$EVAL_SUITE_OUT"
  else
    # Bare `mktemp`: `mktemp -t <template>` is not portable between GNU
    # coreutils and BSD, and this script has to run on a developer's macOS and
    # on a Linux runner alike.
    SUITE="$(mktemp)"
    trap 'rm -f "$SUITE"' EXIT
    echo "note: the run's findings go to a temp file that is deleted on exit;" >&2
    echo "      set EVAL_SUITE_OUT to keep them for inspection." >&2
  fi
  # EVAL_PRICING_ARGS is deliberately word-split: it is an argument list.
  # shellcheck disable=SC2086
  "$PYTHON" -m service.eval_harness run \
    --fixtures "$FIXTURES" \
    --output "$SUITE" \
    ${EVAL_PRICING_ARGS:-}
fi

"$PYTHON" -m service.eval_harness check \
  --suite "$SUITE" \
  --golden "$GOLDEN" \
  --tolerance "$TOLERANCE"
