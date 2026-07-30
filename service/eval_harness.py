"""Run the real review engine against committed fixtures and score the result.

`service/evaluation.py` scores a labeled suite; until now nothing produced the
`observed` half of that suite, so it had to be transcribed by hand from a
Diffuse run. This module closes that gap. It loads fixture pull requests from
`evals/fixtures/`, drives `service.review_engine.generate_review` on the real
call path -- no stubbed `_call_structured`, no synthesized findings -- and emits
exactly the `EvaluationSuite` shape `score_evaluation` already consumes.

Three commands, deliberately split so that only one of them can spend money:

    run       load fixtures, call the review model, write a suite JSON
    capture   score a suite JSON and write it out as a golden
    check     score a suite JSON and compare it against a golden, exit 1 on a
              regression

`capture` and `check` are pure functions of a suite file, so the regression
logic is unit-testable without a credential. `run` is the only command that
reaches a provider.

Goldens are *not* committed. Capturing one requires live model calls, which
means a real credential and a real spend; see `evals/CAPTURE.md`. Until a
golden exists, `check` fails with instructions rather than passing vacuously --
a regression gate that silently succeeds because it has nothing to compare
against is worse than no gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from retriever.retrieve import RetrievedContext
from service import review_engine
from service.diff_parser import parse_unified_diff
from service.evaluation import (
    EvaluationCase,
    EvaluationScore,
    EvaluationSuite,
    ExpectedFinding,
    ModelPricing,
    ObservedFinding,
    score_evaluation,
)
from service.review_models import VerificationBatch

FIXTURE_SCHEMA_VERSION = "diffuse-eval-fixture-v1"
GOLDEN_SCHEMA_VERSION = "diffuse-eval-golden-v1"
DEFAULT_FIXTURE_ROOT = Path("evals/fixtures")
DEFAULT_GOLDEN_PATH = Path("evals/golden/review-baseline.json")
CASE_FILE_NAME = "case.json"

CAPTURE_INSTRUCTIONS = (
    "No golden file at {path}. Goldens record a live review run, so they cannot "
    "be generated offline and none is committed. Follow evals/CAPTURE.md to "
    "capture one against a configured REVIEW_MODEL, review it, and commit it. "
    "Until then this regression gate is not live."
)


class HarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FixtureContext(HarnessModel):
    """One retrieved-context entry, read verbatim from a file in the fixture.

    The content lives in a real file rather than a JSON string so that it stays
    reviewable in a pull request and so that its line numbers are the file's own
    -- a fixture cannot claim a context spans lines it does not have.
    """

    path: str = Field(min_length=1, max_length=512)
    file_path: str = Field(min_length=1, max_length=1024)
    symbol_name: str | None = Field(default=None, max_length=512)
    retrieval_reason: str = Field(default="fixture", min_length=1, max_length=200)
    relevance_score: float = Field(default=0.0, ge=0)
    similarity: float | None = Field(default=None, ge=0, le=1)


class ReviewFixture(HarnessModel):
    """A fixture pull request: a real diff, its context, and its labels."""

    schema_version: Literal["diffuse-eval-fixture-v1"]
    case_id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    diff_path: str = Field(default="diff.patch", min_length=1, max_length=512)
    expected: list[ExpectedFinding] = Field(default_factory=list, max_length=200)
    addressed_finding_ids: list[str] = Field(default_factory=list, max_length=200)
    contexts: list[FixtureContext] = Field(default_factory=list, max_length=50)


@dataclass(frozen=True)
class LoadedFixture:
    fixture: ReviewFixture
    directory: Path
    diff_text: str
    contexts: tuple[RetrievedContext, ...]


class GoldenCase(HarnessModel):
    case_id: str = Field(min_length=1, max_length=200)
    expected_finding_count: int = Field(ge=0)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)


class Golden(HarnessModel):
    """The reference scores a later run must not fall below.

    A golden records *scores*, not model prose. Two runs of the same model
    against the same diff do not produce byte-identical titles or summaries, so
    a byte comparison would fail for reasons that have nothing to do with review
    quality and would be silenced within a week. Findings are compared through
    the scorer -- counts of true and false positives per case, plus the
    aggregate precision, recall and F1 -- which is the thing a threshold change
    in `review_engine.py` actually moves.
    """

    schema_version: Literal["diffuse-eval-golden-v1"]
    suite_name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=512)
    verifier_model: str | None = Field(default=None, min_length=1, max_length=512)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    cases: list[GoldenCase] = Field(min_length=1, max_length=10_000)


class FixtureError(ValueError):
    """A fixture that cannot be trusted to measure anything."""


def _safe_child(directory: Path, relative: str) -> Path:
    """Resolve a fixture-relative path, refusing escapes and symlinks.

    Fixtures are data files, and a suite is exactly the sort of thing someone
    pastes in from elsewhere. `..` and symlinks are how a data file turns into
    an arbitrary-file read, so both are refused here rather than trusted.
    """

    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise FixtureError(f"fixture path must stay inside the fixture: {relative!r}")
    target = directory / relative
    if target.is_symlink() or not target.is_file():
        raise FixtureError(f"fixture path is not a regular file: {relative!r}")
    resolved = target.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise FixtureError(f"fixture path escapes the fixture: {relative!r}")
    return resolved


def validate_fixture(fixture: ReviewFixture, diff_text: str) -> None:
    """Refuse a fixture whose labels the review engine could never satisfy.

    `_deduplicate_candidates` drops any finding that does not land on an added
    or deleted line of the diff, so a label pointing anywhere else is an
    automatic false negative no matter how good the model is. That is precisely
    the defect in `evals/baseline.example.json`: it labels
    `service/webhook.py:42` and supplies no diff at all, which is why the
    committed run scores 0% recall. Catching it at load time keeps the harness
    from reporting a fixture bug as a quality regression.
    """

    parsed = parse_unified_diff(diff_text)
    commentable: dict[str, set[int]] = {}
    for file in parsed.files:
        path = file.comment_path
        if path is None:
            continue
        commentable.setdefault(path, set()).update(file.right_lines | file.left_lines)
    if not commentable:
        raise FixtureError(
            f"fixture {fixture.case_id!r} has a diff with no reviewable changed lines"
        )
    for expected in fixture.expected:
        lines = commentable.get(expected.file_path)
        if lines is None:
            raise FixtureError(
                f"fixture {fixture.case_id!r} labels {expected.file_path!r}, "
                f"which the diff does not change"
            )
        if not any(abs(line - expected.line) <= expected.line_tolerance for line in lines):
            raise FixtureError(
                f"fixture {fixture.case_id!r} label {expected.finding_id!r} points at "
                f"{expected.file_path}:{expected.line}, which is not within "
                f"{expected.line_tolerance} lines of any changed line; the review engine "
                f"can only comment on changed lines, so this label can never be matched"
            )


def load_fixture(directory: Path) -> LoadedFixture:
    case_path = directory / CASE_FILE_NAME
    if case_path.is_symlink() or not case_path.is_file():
        raise FixtureError(f"missing {CASE_FILE_NAME} in {directory}")
    try:
        fixture = ReviewFixture.model_validate_json(case_path.read_text())
    except (OSError, ValidationError) as error:
        raise FixtureError(f"invalid fixture {directory.name}: {error}") from error
    if fixture.case_id != directory.name:
        raise FixtureError(
            f"fixture {directory.name!r} declares case_id {fixture.case_id!r}; the "
            f"directory name is the case id so goldens cannot drift from fixtures"
        )
    diff_text = _safe_child(directory, fixture.diff_path).read_text()
    if not diff_text.strip():
        raise FixtureError(f"fixture {fixture.case_id!r} has an empty diff")
    validate_fixture(fixture, diff_text)
    contexts = []
    for entry in fixture.contexts:
        content = _safe_child(directory, entry.path).read_text()
        line_count = len(content.splitlines()) or 1
        contexts.append(
            RetrievedContext(
                file_path=entry.file_path,
                symbol_name=entry.symbol_name,
                start_line=1,
                end_line=line_count,
                content=content,
                similarity=entry.similarity,
                retrieval_reason=entry.retrieval_reason,
                relevance_score=entry.relevance_score,
            )
        )
    return LoadedFixture(
        fixture=fixture,
        directory=directory,
        diff_text=diff_text,
        contexts=tuple(contexts),
    )


def load_fixtures(root: Path) -> list[LoadedFixture]:
    if not root.is_dir():
        raise FixtureError(f"fixture directory not found: {root}")
    directories = sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not child.is_symlink() and (child / CASE_FILE_NAME).is_file()
    )
    if not directories:
        raise FixtureError(f"no fixtures under {root}")
    return [load_fixture(directory) for directory in directories]


@dataclass
class _TokenSplit:
    """Candidate- and verifier-stage tokens, observed rather than assumed."""

    candidate_prompt: int = 0
    candidate_completion: int = 0
    verifier_prompt: int = 0
    verifier_completion: int = 0


@contextmanager
def _observe_token_split() -> Iterator[_TokenSplit]:
    """Attribute each structured call's tokens to the stage that made it.

    `ReviewReport` carries one combined `prompt_tokens`/`completion_tokens`
    pair, but `EvaluationSuite` prices candidate and verification tokens
    separately -- deliberately, because a cross-family pair does not share a
    rate card and folding the two together produces a cost figure that looks
    authoritative and is not.

    So the split has to be observed. This wraps `_call_structured` and
    delegates to the real one: it changes no argument, no return value and no
    control flow, it only records which stage each call belonged to. The
    verification stage is the single `VerificationBatch` call; the candidate
    passes and the diagram both run on the candidate model.

    The tidier fix is for `generate_review` to return the split itself. That is
    a change to `ReviewReport`, which this unit is not scoped to make; see the
    pull request description.
    """

    split = _TokenSplit()
    original = review_engine._call_structured

    def recording(response_model, **kwargs):
        verifier = response_model is VerificationBatch
        try:
            result, prompt_tokens, completion_tokens = original(response_model, **kwargs)
        except review_engine.StructuredOutputValidationError as error:
            # The diagram stage swallows this and keeps its token cost, so the
            # harness has to keep it too or the recorded spend is short.
            if verifier:
                split.verifier_prompt += error.prompt_tokens
                split.verifier_completion += error.completion_tokens
            else:
                split.candidate_prompt += error.prompt_tokens
                split.candidate_completion += error.completion_tokens
            raise
        if verifier:
            split.verifier_prompt += prompt_tokens
            split.verifier_completion += completion_tokens
        else:
            split.candidate_prompt += prompt_tokens
            split.candidate_completion += completion_tokens
        return result, prompt_tokens, completion_tokens

    review_engine._call_structured = recording
    try:
        yield split
    finally:
        review_engine._call_structured = original


def run_fixture(
    loaded: LoadedFixture,
    *,
    candidate_model: str,
    verifier_model: str,
    progress_callback: Callable[[], None] | None = None,
) -> EvaluationCase:
    """Review one fixture on the real call path and return a scoreable case."""

    started = time.monotonic()
    with _observe_token_split() as split:
        report = review_engine.generate_review(
            loaded.diff_text,
            list(loaded.contexts),
            progress_callback=progress_callback,
            candidate_model=candidate_model,
            verifier_model=verifier_model,
        )
    latency_ms = max(0, round((time.monotonic() - started) * 1000))
    observed = [
        ObservedFinding(
            file_path=finding.file_path,
            line=finding.line,
            category=finding.category,
            severity=finding.severity,
            fingerprint=finding.fingerprint,
        )
        for finding in report.findings
    ]
    return EvaluationCase(
        case_id=loaded.fixture.case_id,
        expected=loaded.fixture.expected,
        observed=observed,
        addressed_finding_ids=loaded.fixture.addressed_finding_ids,
        latency_ms=latency_ms,
        prompt_tokens=split.candidate_prompt,
        completion_tokens=split.candidate_completion,
        verifier_prompt_tokens=split.verifier_prompt,
        verifier_completion_tokens=split.verifier_completion,
    )


def run_suite(
    fixtures: list[LoadedFixture],
    *,
    name: str,
    candidate_model: str,
    verifier_model: str,
    pricing: ModelPricing,
    verifier_pricing: ModelPricing | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> EvaluationSuite:
    """Review every fixture and assemble the suite `score_evaluation` consumes."""

    cases = [
        run_fixture(
            loaded,
            candidate_model=candidate_model,
            verifier_model=verifier_model,
            progress_callback=progress_callback,
        )
        for loaded in fixtures
    ]
    return EvaluationSuite(
        schema_version="diffuse-evaluation-v1",
        name=name,
        model=candidate_model,
        pricing=pricing,
        verifier_model=verifier_model,
        verifier_pricing=verifier_pricing,
        cases=cases,
    )


def golden_from_score(score: EvaluationScore) -> Golden:
    by_case = {case.case_id: case for case in score.cases}
    return Golden(
        schema_version=GOLDEN_SCHEMA_VERSION,
        suite_name=score.suite_name,
        model=score.model,
        verifier_model=score.verifier_model,
        precision=score.precision,
        recall=score.recall,
        f1=score.f1,
        cases=[
            GoldenCase(
                case_id=case_id,
                expected_finding_count=case.true_positives + case.false_negatives,
                true_positives=case.true_positives,
                false_positives=case.false_positives,
                false_negatives=case.false_negatives,
            )
            for case_id, case in sorted(by_case.items())
        ],
    )


def compare_to_golden(
    score: EvaluationScore,
    golden: Golden,
    *,
    tolerance: float = 0.0,
) -> list[str]:
    """Every way the run is worse than the golden. Empty means no regression."""

    if not 0 <= tolerance <= 1:
        raise ValueError("tolerance must be between 0 and 1")
    regressions: list[str] = []
    observed_cases = {case.case_id: case for case in score.cases}
    golden_cases = {case.case_id: case for case in golden.cases}

    for case_id in sorted(golden_cases.keys() - observed_cases.keys()):
        regressions.append(f"case {case_id!r} is in the golden but was not run")
    for case_id in sorted(observed_cases.keys() - golden_cases.keys()):
        # Not a quality regression, but the gate no longer covers what it
        # claims to. Recapture rather than let a fixture ride along unmeasured.
        regressions.append(
            f"case {case_id!r} has no golden entry; recapture the golden "
            f"(see evals/CAPTURE.md)"
        )
    for case_id in sorted(golden_cases.keys() & observed_cases.keys()):
        observed = observed_cases[case_id]
        reference = golden_cases[case_id]
        if observed.false_negatives > reference.false_negatives:
            regressions.append(
                f"case {case_id!r} missed {observed.false_negatives} labeled findings, "
                f"golden missed {reference.false_negatives}"
            )
        if observed.false_positives > reference.false_positives:
            regressions.append(
                f"case {case_id!r} reported {observed.false_positives} unlabeled findings, "
                f"golden reported {reference.false_positives}"
            )

    for metric, current, reference in (
        ("precision", score.precision, golden.precision),
        ("recall", score.recall, golden.recall),
        ("f1", score.f1, golden.f1),
    ):
        if current < reference - tolerance:
            regressions.append(
                f"{metric} fell to {current:.4f} from a golden {reference:.4f} "
                f"(tolerance {tolerance:.4f})"
            )
    if golden.model != score.model:
        regressions.append(
            f"golden was captured against model {golden.model!r} but this run used "
            f"{score.model!r}; the comparison is not meaningful"
        )
    if golden.verifier_model != score.verifier_model:
        regressions.append(
            f"golden was captured against verifier {golden.verifier_model!r} but this "
            f"run used {score.verifier_model!r}; the comparison is not meaningful"
        )
    return regressions


def load_suite(path: Path) -> EvaluationSuite:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evaluation suite must be a regular file: {path}")
    try:
        return EvaluationSuite.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid evaluation suite: {error}") from error


def load_golden(path: Path) -> Golden:
    if not path.exists():
        raise FileNotFoundError(CAPTURE_INSTRUCTIONS.format(path=path))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"golden must be a regular file: {path}")
    try:
        return Golden.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid golden: {error}") from error


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _pricing(prefix: str, args: argparse.Namespace) -> ModelPricing | None:
    input_rate = getattr(args, f"{prefix}input_usd_per_million")
    output_rate = getattr(args, f"{prefix}output_usd_per_million")
    if input_rate is None and output_rate is None:
        return None
    return ModelPricing(
        input_usd_per_million_tokens=input_rate or 0,
        output_usd_per_million_tokens=output_rate or 0,
    )


def _run(args: argparse.Namespace) -> None:
    fixtures = load_fixtures(args.fixtures)
    # No default model, on purpose: these raise and name the variable when
    # REVIEW_MODEL is unset. See service/review_engine.review_model.
    candidate_model = review_engine.review_model()
    verifier_model = review_engine.review_verifier_model()
    pricing = _pricing("", args) or ModelPricing()
    verifier_pricing = _pricing("verifier_", args)
    # `EvaluationSuite` refuses a cross-family pair with only one rate card, and
    # it is assembled after every model call has already been paid for. Check
    # the same condition first so the refusal costs nothing.
    if verifier_model != candidate_model and verifier_pricing is None:
        raise ValueError(
            f"the verifier model ({verifier_model}) differs from the candidate model "
            f"({candidate_model}), so it bills at its own rate. Pass "
            f"--verifier-input-usd-per-million and --verifier-output-usd-per-million, "
            f"or set REVIEW_VERIFIER_MODEL to the candidate model."
        )
    print(
        f"reviewing {len(fixtures)} fixtures with candidate={candidate_model} "
        f"verifier={verifier_model}",
        file=sys.stderr,
    )
    suite = run_suite(
        fixtures,
        name=args.name,
        candidate_model=candidate_model,
        verifier_model=verifier_model,
        pricing=pricing,
        verifier_pricing=verifier_pricing,
    )
    _write_json(args.output, suite.model_dump(mode="json"))
    print(f"wrote {args.output}", file=sys.stderr)


def _capture(args: argparse.Namespace) -> None:
    score = score_evaluation(load_suite(args.suite))
    _write_json(args.golden, golden_from_score(score).model_dump(mode="json"))
    print(
        f"wrote {args.golden}: precision={score.precision:.4f} "
        f"recall={score.recall:.4f} f1={score.f1:.4f}",
        file=sys.stderr,
    )


def _check(args: argparse.Namespace) -> None:
    score = score_evaluation(load_suite(args.suite))
    golden = load_golden(args.golden)
    regressions = compare_to_golden(score, golden, tolerance=args.tolerance)
    print(json.dumps(score.model_dump(mode="json"), indent=2, sort_keys=True))
    if regressions:
        print("\nREGRESSION against " + str(args.golden) + ":", file=sys.stderr)
        for regression in regressions:
            print(f"  - {regression}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"\nno regression against {args.golden} "
        f"(precision={score.precision:.4f} recall={score.recall:.4f} f1={score.f1:.4f})",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m service.eval_harness",
        description=(
            "Run the Diffuse review engine against committed fixtures and compare "
            "the scored result against a golden."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help="Review every fixture with the configured model and write a suite JSON",
        description=(
            "Calls the review model once per fixture. This is the only command "
            "that spends money. REVIEW_MODEL must be set; there is no default."
        ),
    )
    run.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_ROOT)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--name", default="diffuse-review-fixtures")
    run.add_argument(
        "--input-usd-per-million",
        type=float,
        default=None,
        help=(
            "Candidate input rate. Omitted means unpriced: estimated_cost_usd is "
            "reported as 0 rather than guessed."
        ),
    )
    run.add_argument("--output-usd-per-million", type=float, default=None)
    run.add_argument(
        "--verifier-input-usd-per-million",
        type=float,
        default=None,
        help=(
            "Required by service/evaluation.py whenever the verifier model differs "
            "from the candidate model, which bills at its own rate."
        ),
    )
    run.add_argument("--verifier-output-usd-per-million", type=float, default=None)
    run.set_defaults(handler=_run)

    capture = subparsers.add_parser(
        "capture",
        help="Turn a suite JSON into a golden. Offline; no model calls.",
    )
    capture.add_argument("--suite", type=Path, required=True)
    capture.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    capture.set_defaults(handler=_capture)

    check = subparsers.add_parser(
        "check",
        help="Score a suite JSON against a golden and exit 1 on a regression",
    )
    check.add_argument("--suite", type=Path, required=True)
    check.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    check.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help=(
            "Allowed absolute drop in precision, recall and F1 before the run "
            "counts as a regression (default: 0, no slack)."
        ),
    )
    check.set_defaults(handler=_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (FixtureError, FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
