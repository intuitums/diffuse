"""CLI entry point for Diffuse review quality evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from service.evaluation import EvaluationSuite, score_evaluation


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
    parser.add_argument("--min-precision", type=float, default=0)
    parser.add_argument("--min-recall", type=float, default=0)
    parser.add_argument("--min-f1", type=float, default=0)
    parser.set_defaults(handler=_run)
