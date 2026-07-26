"""CLI entry point for Diffuse review quality evaluation."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from pydantic import ValidationError

from service.evaluation import EvaluationSuite, score_evaluation


def _threshold(value: str) -> float:
    """Parse a quality gate, rejecting values that cannot gate anything.

    ``float("nan")`` parses, and every comparison against it is false, so
    ``--min-f1 nan`` would let a release pass its own quality gate no matter
    what the suite scored. Infinities are the mirror image: they fail
    unconditionally. Both are configuration mistakes, not thresholds.
    """

    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from error
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a finite value between 0 and 1"
        )
    return number


def _load_suite(path: Path) -> EvaluationSuite:
    if path.is_symlink() or not path.is_file():
        raise ValueError("evaluation suite must be a regular file")
    try:
        return EvaluationSuite.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid evaluation suite: {error}") from error


def _run(args: argparse.Namespace) -> None:
    score = score_evaluation(_load_suite(args.suite))
    print(json.dumps(score.model_dump(mode="json"), indent=2, sort_keys=True))
    failed = (
        score.precision < args.min_precision
        or score.recall < args.min_recall
        or score.f1 < args.min_f1
    )
    if failed:
        raise SystemExit(1)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("suite", type=Path)
    parser.add_argument("--min-precision", type=_threshold, default=0)
    parser.add_argument("--min-recall", type=_threshold, default=0)
    parser.add_argument("--min-f1", type=_threshold, default=0)
    parser.set_defaults(handler=_run)
