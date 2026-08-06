"""Run the real review engine against committed fixtures and score the result.

`service/evaluation.py` scores a labeled suite; until now nothing produced the
`observed` half of that suite, so it had to be transcribed by hand from a
Diffuse run. This module closes that gap. It loads fixture pull requests from
`evals/fixtures/`, drives `service.review.engine.generate_review` on the real
call path -- no stubbed `_call_structured`, no synthesized findings -- and emits
exactly the `EvaluationSuite` shape `score_evaluation` already consumes.

Three commands, deliberately split so that only one of them can spend money:

    run       load fixtures, call the review model, write a suite JSON
    capture   score a suite JSON and write it out as a baseline
    check     score a suite JSON and compare it against a baseline, exit 1 on a
              regression

`capture` and `check` are pure functions of a suite file, so the regression
logic is unit-testable without a credential. `run` is the only command that
reaches a provider.

Baselines are *not* committed. Capturing one requires live model calls, which
means a real credential and a real spend; see `evals/CAPTURE.md`. Until a
baseline exists, `check` fails with instructions rather than passing vacuously --
a regression gate that silently succeeds because it has nothing to compare
against is worse than no gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from retriever.retrieve import RetrievedContext
from service.diff_parser import parse_unified_diff
from service.evaluation import (
    EvaluationCase,
    EvaluationScore,
    EvaluationSuite,
    ExpectedFinding,
    ModelPricing,
    ObservedFinding,
    ReviewDepthRendering,
    RunConfiguration,
    score_evaluation,
)
from service.review import engine as review_engine

#: Bumped from v1 because fixture labels now require semantic title text and an
#: exact diff side; neither signal exists in a v1 fixture.
FIXTURE_SCHEMA_VERSION = "diffuse-eval-fixture-v2"
#: Bumped from v2 because a baseline captured by the location-only matcher can
#: credit observations the title-and-side matcher correctly rejects. Reusing its
#: scores would silently reinterpret the reference run, so it must be recaptured.
#:
#: Unrelated to the "baseline" in `service/storage/migrations.py`, which names
#: the frozen version-1 SQL schema. This one versions the eval harness's
#: recorded reference run; the two never meet.
BASELINE_SCHEMA_VERSION = "diffuse-eval-baseline-v3"
DEFAULT_FIXTURE_ROOT = Path("evals/fixtures")
DEFAULT_BASELINE_PATH = Path("evals/baselines/review-baseline.json")
CASE_FILE_NAME = "case.json"

CAPTURE_INSTRUCTIONS = (
    "No baseline file at {path}. Baselines record a live review run, so they cannot "
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


class ReviewFixture(HarnessModel):
    """A fixture pull request: a real diff, its context, and its labels."""

    schema_version: Literal["diffuse-eval-fixture-v2"]
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
    #: SHA-256 over `diff.patch` and `case.json` as they were read.
    digest: str


class BaselineCase(HarnessModel):
    case_id: str = Field(min_length=1, max_length=200)
    expected_finding_count: int = Field(ge=0)
    #: The fixture content this case was captured against. The label count
    #: catches an edited label set; this catches an edited *diff*, which moves
    #: the score just as far and was previously undefended -- make a bug more
    #: obvious and recall rises while the baseline keeps passing, so the gate now
    #: measures an easier task than the one it was calibrated on.
    fixture_digest: str | None = Field(default=None, min_length=1, max_length=128)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)


class Baseline(HarnessModel):
    """The reference scores a later run must not fall below.

    A baseline records *scores*, not model prose. Titles participate in the
    scorer through normalized token overlap; they are not compared byte for
    byte, because two runs of the same model need not use identical wording.
    Findings are compared through counts of true and false positives per case,
    plus aggregate precision, recall and F1 -- the measurements a threshold
    change in `service/review/engine.py` actually moves.

    It also records the conditions the scores were produced under, because a
    score is only a standard while those hold. The model name was pinned from
    the start; `run_configuration` pins the rest -- prompt version, confidence
    floor, review passes, and both the requested and the *resolved* review
    depth -- and `BaselineCase.fixture_digest` pins the fixture content. Every one
    of those moves the score, and every one of them was previously free to drift
    under a baseline that kept passing.
    """

    schema_version: Literal["diffuse-eval-baseline-v3"]
    suite_name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=512)
    verifier_model: str | None = Field(default=None, min_length=1, max_length=512)
    run_configuration: RunConfiguration
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    #: Recorded and reported as a delta, never gated. Category disagreement is a
    #: taxonomy signal, not a detection signal, and it is also non-minimal by
    #: construction (see `_score_case`), so failing a deletion on it would be
    #: wrong twice over. Absent from the baseline entirely, though, drift is
    #: invisible -- a reviewer that starts filing every injection as
    #: `maintainability` passes unchanged.
    category_mismatches: int = Field(default=0, ge=0)
    cases: list[BaselineCase] = Field(min_length=1, max_length=10_000)


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
    the defect in `evals/baseline.example.json` -- which, despite the name, is a
    hand-written example suite for `diffuse evaluate`, not a captured baseline:
    it labels `service/webhook.py:42` and supplies no diff at all, which is why
    the run recorded in it scores 0% recall. Catching it at load time keeps the
    harness from reporting a fixture bug as a quality regression.
    """

    parsed = parse_unified_diff(diff_text)
    commentable: dict[tuple[str, str], set[int]] = {}
    for file in parsed.files:
        path = file.comment_path
        if path is None:
            continue
        commentable[path, "LEFT"] = file.left_lines
        commentable[path, "RIGHT"] = file.right_lines
    if not commentable:
        raise FixtureError(
            f"fixture {fixture.case_id!r} has a diff with no reviewable changed lines"
        )
    for expected in fixture.expected:
        file_is_changed = any(path == expected.file_path for path, _side in commentable)
        if not file_is_changed:
            raise FixtureError(
                f"fixture {fixture.case_id!r} labels {expected.file_path!r}, "
                f"which the diff does not change"
            )
        lines = commentable[expected.file_path, expected.side]
        if not any(abs(line - expected.line) <= expected.line_tolerance for line in lines):
            raise FixtureError(
                f"fixture {fixture.case_id!r} label {expected.finding_id!r} points at "
                f"{expected.file_path}:{expected.line} ({expected.side}), which is not within "
                f"{expected.line_tolerance} lines of any changed line; the review engine "
                f"can only comment on changed lines, so this label can never be matched"
            )


def fixture_digest(case_json: str, diff_text: str) -> str:
    """A content hash over the two files that decide what a case measures.

    Length-prefixed rather than concatenated, so moving a byte from the end of
    one file to the start of the other cannot leave the digest unchanged.
    """

    digest = hashlib.sha256()
    for part in (case_json, diff_text):
        encoded = part.encode()
        digest.update(str(len(encoded)).encode())
        digest.update(b"\0")
        digest.update(encoded)
    return digest.hexdigest()


def load_fixture(directory: Path) -> LoadedFixture:
    case_path = directory / CASE_FILE_NAME
    if case_path.is_symlink() or not case_path.is_file():
        raise FixtureError(f"missing {CASE_FILE_NAME} in {directory}")
    case_json = case_path.read_text()
    try:
        fixture = ReviewFixture.model_validate_json(case_json)
    except (OSError, ValidationError) as error:
        raise FixtureError(f"invalid fixture {directory.name}: {error}") from error
    if fixture.case_id != directory.name:
        raise FixtureError(
            f"fixture {directory.name!r} declares case_id {fixture.case_id!r}; the "
            f"directory name is the case id so baselines cannot drift from fixtures"
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
                retrieval_reason=entry.retrieval_reason,
                relevance_score=entry.relevance_score,
            )
        )
    return LoadedFixture(
        fixture=fixture,
        directory=directory,
        diff_text=diff_text,
        contexts=tuple(contexts),
        digest=fixture_digest(case_json, diff_text),
    )


def load_fixtures(root: Path) -> list[LoadedFixture]:
    """Every fixture under `root`. A directory that is not one is an error.

    Skipping a directory without a `case.json` would make the suite silently
    shrink: rename one file and the run covers seven cases instead of eight with
    nothing red. The baseline comparison catches that afterwards, but only once a
    baseline exists, and the fixture set is the thing the baseline is captured
    from.
    """

    if not root.is_dir():
        raise FixtureError(f"fixture directory not found: {root}")
    directories = sorted(
        child for child in root.iterdir() if child.is_dir() and not child.is_symlink()
    )
    if not directories:
        raise FixtureError(f"no fixtures under {root}")
    return [load_fixture(directory) for directory in directories]


def run_fixture(
    loaded: LoadedFixture,
    *,
    candidate_model: str,
    verifier_model: str,
    progress_callback: Callable[[], None] | None = None,
) -> EvaluationCase:
    """Review one fixture on the real call path and return a scoreable case."""

    started = time.monotonic()
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
            title=finding.title,
            file_path=finding.file_path,
            line=finding.line,
            side=finding.side,
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
        prompt_tokens=report.prompt_tokens - report.verifier_prompt_tokens,
        completion_tokens=report.completion_tokens - report.verifier_completion_tokens,
        verifier_prompt_tokens=report.verifier_prompt_tokens,
        verifier_completion_tokens=report.verifier_completion_tokens,
        fixture_digest=loaded.digest,
    )


def resolve_run_configuration(
    support: review_engine.ReviewDepthSupport | None = None,
) -> RunConfiguration:
    """Read every environment value that moves the score, and what depth resolved to.

    `resolve_review_depth_support` is the same call the worker makes at startup
    and `service/cli/review.py` makes before its first model call. The harness has to make
    it too, and for a second reason beyond reporting: a candidate model that
    cannot express the requested depth is sent *nothing*, so a baseline captured
    that way would record a depth it never used. `evals/CAPTURE.md` recommends
    capturing on a cheap model first, and the cheap model it names --
    `openai/gpt-4.1-mini` -- is exactly that case.
    """

    if support is None:
        support = review_engine.resolve_review_depth_support()
    refusal = support.refusal()
    if refusal is not None:
        raise ValueError(refusal)
    from service.review.runtimes import review_runtime_name

    return RunConfiguration(
        prompt_version=review_engine.PROMPT_VERSION,
        min_review_confidence=review_engine.minimum_review_confidence(),
        review_passes=list(review_engine.review_passes()),
        review_runtime=review_runtime_name(),
        requested_review_depth=support.depth,
        depth_renderings=[
            ReviewDepthRendering(
                stage=stage,
                model=plan.model,
                mechanism=plan.mechanism.value,
                effort=plan.effort,
            )
            for stage, plan in support.plans
        ],
    )


def run_suite(
    fixtures: list[LoadedFixture],
    *,
    name: str,
    candidate_model: str,
    verifier_model: str,
    pricing: ModelPricing,
    verifier_pricing: ModelPricing | None = None,
    run_configuration: RunConfiguration | None = None,
    progress_callback: Callable[[], None] | None = None,
    completed: dict[str, EvaluationCase] | None = None,
    on_case: Callable[[EvaluationCase], None] | None = None,
) -> EvaluationSuite:
    """Review every fixture and assemble the suite `score_evaluation` consumes.

    `completed` supplies cases a previous attempt already paid for, and `on_case`
    is called as each new one finishes. A full capture is 40 serial model calls
    over 40-80 minutes; a provider error on the seventh of eight fixtures used to
    discard all of it.
    """

    already_done = completed or {}
    cases: list[EvaluationCase] = []
    for loaded in fixtures:
        reusable = already_done.get(loaded.fixture.case_id)
        if reusable is not None and reusable.fixture_digest == loaded.digest:
            cases.append(reusable)
            continue
        case = run_fixture(
            loaded,
            candidate_model=candidate_model,
            verifier_model=verifier_model,
            progress_callback=progress_callback,
        )
        cases.append(case)
        if on_case is not None:
            on_case(case)
    return EvaluationSuite(
        schema_version="diffuse-evaluation-v3",
        name=name,
        model=candidate_model,
        pricing=pricing,
        verifier_model=verifier_model,
        verifier_pricing=verifier_pricing,
        run_configuration=run_configuration,
        cases=cases,
    )


def baseline_from_score(score: EvaluationScore) -> Baseline:
    if score.run_configuration is None:
        raise ValueError(
            "this suite records no run configuration, so a baseline captured from it "
            "could not refuse a later run at a different confidence floor, prompt "
            "version, review-pass set or review depth. Produce the suite with "
            "`python -m service.eval_harness run`."
        )
    by_case = {case.case_id: case for case in score.cases}
    return Baseline(
        schema_version=BASELINE_SCHEMA_VERSION,
        suite_name=score.suite_name,
        model=score.model,
        verifier_model=score.verifier_model,
        run_configuration=score.run_configuration,
        precision=score.precision,
        recall=score.recall,
        f1=score.f1,
        category_mismatches=score.category_mismatches,
        cases=[
            BaselineCase(
                case_id=case_id,
                expected_finding_count=case.true_positives + case.false_negatives,
                fixture_digest=case.fixture_digest,
                true_positives=case.true_positives,
                false_positives=case.false_positives,
                false_negatives=case.false_negatives,
            )
            for case_id, case in sorted(by_case.items())
        ],
    )


def category_mismatch_delta(score: EvaluationScore, baseline: Baseline) -> str | None:
    """How the category confusion moved, or None when it did not.

    Deliberately not a regression. Category disagreement says something about
    the reviewer's taxonomy, not about whether it found the bug, and the table
    itself is non-minimal by construction (see `_score_case`), so failing a
    deletion on it would be wrong on both counts. Reported so the drift is
    legible rather than invisible.
    """

    if score.category_mismatches == baseline.category_mismatches:
        return None
    direction = (
        "up from"
        if score.category_mismatches > baseline.category_mismatches
        else "down from"
    )
    detail = ", ".join(
        f"{item.expected.value}->{item.observed.value} x{item.count}"
        for item in score.category_confusion
    )
    return (
        f"category mismatches {direction} the baseline: "
        f"{score.category_mismatches} vs {baseline.category_mismatches}"
        + (f" ({detail})" if detail else "")
    )


def compare_to_baseline(
    score: EvaluationScore,
    baseline: Baseline,
    *,
    tolerance: float = 0.0,
) -> list[str]:
    """Every way the run is worse than the baseline. Empty means no regression."""

    if not 0 <= tolerance <= 1:
        raise ValueError("tolerance must be between 0 and 1")
    regressions: list[str] = []
    observed_cases = {case.case_id: case for case in score.cases}
    baseline_cases = {case.case_id: case for case in baseline.cases}

    for case_id in sorted(baseline_cases.keys() - observed_cases.keys()):
        regressions.append(f"case {case_id!r} is in the baseline but was not run")
    for case_id in sorted(observed_cases.keys() - baseline_cases.keys()):
        # Not a quality regression, but the gate no longer covers what it
        # claims to. Recapture rather than let a fixture ride along unmeasured.
        regressions.append(
            f"case {case_id!r} has no baseline entry; recapture the baseline "
            f"(see evals/CAPTURE.md)"
        )
    for case_id in sorted(baseline_cases.keys() & observed_cases.keys()):
        observed = observed_cases[case_id]
        reference = baseline_cases[case_id]
        labeled = observed.true_positives + observed.false_negatives
        if labeled != reference.expected_finding_count:
            # Editing a fixture's labels after capture silently rebases the
            # comparison: drop a label the engine kept missing and recall
            # "improves" without the engine changing at all.
            regressions.append(
                f"case {case_id!r} now carries {labeled} labels but the baseline was "
                f"captured against {reference.expected_finding_count}; recapture the "
                f"baseline (see evals/CAPTURE.md)"
            )
        if (
            reference.fixture_digest is not None
            and observed.fixture_digest != reference.fixture_digest
        ):
            # Editing `diff.patch` rebases the comparison exactly as editing the
            # labels does, and the label count cannot see it: make the bug more
            # obvious and recall rises while the baseline keeps passing, so the
            # gate now measures an easier task than the one it was calibrated
            # on.
            regressions.append(
                f"case {case_id!r} was captured against different fixture content "
                f"(digest {reference.fixture_digest[:12]}, now "
                f"{(observed.fixture_digest or 'none')[:12]}); recapture the baseline "
                f"(see evals/CAPTURE.md)"
            )
        if observed.false_negatives > reference.false_negatives:
            regressions.append(
                f"case {case_id!r} missed {observed.false_negatives} labeled findings, "
                f"the baseline missed {reference.false_negatives}"
            )
        if observed.false_positives > reference.false_positives:
            regressions.append(
                f"case {case_id!r} reported {observed.false_positives} unlabeled findings, "
                f"the baseline reported {reference.false_positives}"
            )

    for metric, current, reference in (
        ("precision", score.precision, baseline.precision),
        ("recall", score.recall, baseline.recall),
        ("f1", score.f1, baseline.f1),
    ):
        if current < reference - tolerance:
            regressions.append(
                f"{metric} fell to {current:.4f} from the baseline's {reference:.4f} "
                f"(tolerance {tolerance:.4f})"
            )
    if baseline.model != score.model:
        regressions.append(
            f"the baseline was captured against model {baseline.model!r} but this run "
            f"used {score.model!r}; the comparison is not meaningful"
        )
    if baseline.verifier_model != score.verifier_model:
        regressions.append(
            f"the baseline was captured against verifier {baseline.verifier_model!r} "
            f"but this run used {score.verifier_model!r}; the comparison is not meaningful"
        )
    # The model name was the only condition the baseline used to pin. Everything
    # here is read from the environment at call time and moves the score just as
    # far: capture with MIN_REVIEW_CONFIDENCE=0.99 left in the shell and the
    # baseline records near-zero recall and near-zero false positives that every
    # later run at the default 0.75 clears trivially, forever. Refused exactly
    # as a different model is refused.
    if score.run_configuration is None:
        regressions.append(
            "this run recorded no configuration, so it cannot be shown to have used "
            "the same confidence floor, prompt version, review passes and review "
            "depth as the baseline; the comparison is not meaningful"
        )
    else:
        for difference in score.run_configuration.differences(baseline.run_configuration):
            regressions.append(
                f"the baseline was captured under a different configuration: "
                f"{difference}; the comparison is not meaningful"
            )
    return regressions


def load_suite(path: Path) -> EvaluationSuite:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evaluation suite must be a regular file: {path}")
    try:
        return EvaluationSuite.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid evaluation suite: {error}") from error


def load_baseline(path: Path) -> Baseline:
    if not path.exists():
        raise FileNotFoundError(CAPTURE_INSTRUCTIONS.format(path=path))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"baseline must be a regular file: {path}")
    try:
        return Baseline.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid baseline: {error}") from error


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


def _resume_cases(path: Path, fixtures: list[LoadedFixture]) -> dict[str, EvaluationCase]:
    """Cases a previous attempt already paid for, keyed by case id.

    A partial file is expected here -- it is written after every fixture
    precisely so an interrupted run leaves one -- so it is parsed leniently and
    a case whose fixture has since changed is dropped rather than reused.
    """

    if not path.is_file() or path.is_symlink():
        return {}
    digests = {loaded.fixture.case_id: loaded.digest for loaded in fixtures}
    try:
        payload = json.loads(path.read_text())
        raw_cases = payload["cases"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    resumed: dict[str, EvaluationCase] = {}
    for entry in raw_cases if isinstance(raw_cases, list) else []:
        try:
            case = EvaluationCase.model_validate(entry)
        except ValidationError:
            continue
        if case.fixture_digest == digests.get(case.case_id):
            resumed[case.case_id] = case
    return resumed


def _run(args: argparse.Namespace) -> None:
    fixtures = load_fixtures(args.fixtures)
    # No default model, on purpose: these raise and name the variable when
    # REVIEW_MODEL is unset. See service/review/engine.review_model.
    candidate_model = review_engine.review_model()
    verifier_model = review_engine.review_verifier_model()
    # Names what each model will actually be sent, and refuses a depth the
    # candidate cannot express -- before anything is billed, and before the
    # baseline can record a depth that was never sent.
    depth_support = review_engine.resolve_review_depth_support()
    for line in depth_support.report_lines():
        print(line, file=sys.stderr)
    run_configuration = resolve_run_configuration(depth_support)
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
    completed = _resume_cases(args.output, fixtures) if args.resume else {}
    if completed:
        print(
            f"resuming: reusing {len(completed)} case(s) already in {args.output}",
            file=sys.stderr,
        )
    done: list[EvaluationCase] = []

    def _record(case: EvaluationCase) -> None:
        # Written after every fixture, not only at the end. A provider error on
        # the seventh of eight fixtures otherwise discards 35 completed model
        # calls -- 40-80 minutes and several dollars.
        done.append(case)
        _write_json(
            args.output,
            {
                "schema_version": "diffuse-evaluation-v3",
                "name": args.name,
                "model": candidate_model,
                "cases": [case.model_dump(mode="json") for case in done],
                "partial": True,
            },
        )

    for case in completed.values():
        done.append(case)
    suite = run_suite(
        fixtures,
        name=args.name,
        candidate_model=candidate_model,
        verifier_model=verifier_model,
        pricing=pricing,
        verifier_pricing=verifier_pricing,
        run_configuration=run_configuration,
        completed=completed,
        on_case=_record,
    )
    _write_json(args.output, suite.model_dump(mode="json"))
    print(f"wrote {args.output}", file=sys.stderr)


def _capture(args: argparse.Namespace) -> None:
    score = score_evaluation(load_suite(args.suite))
    if score.true_positives == 0 and not args.allow_zero_recall:
        # A baseline with no true positives is structurally valid and passes
        # against *any* later run -- including one where the engine returns
        # nothing at all, because precision is 1.0 when TP+FP is 0. That is a
        # gate that cannot fail, which is the one thing this harness exists not
        # to be. Refused here rather than warned about in prose.
        raise ValueError(
            f"this suite found none of its {score.expected_finding_count} labeled "
            f"defects, so a baseline captured from it would pass against every later "
            f"run, including one that reports nothing at all. Read the findings "
            f"before deciding what this means; a real zero is a finding about the "
            f"review engine, not a reason to weaken the labels (evals/CAPTURE.md). "
            f"Pass --allow-zero-recall to record it anyway."
        )
    _write_json(args.baseline, baseline_from_score(score).model_dump(mode="json"))
    print(
        f"wrote {args.baseline}: precision={score.precision:.4f} "
        f"recall={score.recall:.4f} f1={score.f1:.4f} "
        f"category_mismatches={score.category_mismatches} "
        f"severity_mismatches={score.severity_mismatches}",
        file=sys.stderr,
    )


def _check(args: argparse.Namespace) -> None:
    score = score_evaluation(load_suite(args.suite))
    baseline = load_baseline(args.baseline)
    regressions = compare_to_baseline(score, baseline, tolerance=args.tolerance)
    print(json.dumps(score.model_dump(mode="json"), indent=2, sort_keys=True))
    drift = category_mismatch_delta(score, baseline)
    if drift is not None:
        print(f"\nnote: {drift}", file=sys.stderr)
        print(
            "      taxonomy drift is reported, not gated: it says what the reviewer "
            "called the defect, not whether it found it.",
            file=sys.stderr,
        )
    if score.severity_mismatches:
        print(
            f"\nnote: {score.severity_mismatches} labeled finding(s) were located and "
            f"graded differently. The severity gate charges each of those as a false "
            f"negative and a false positive; see evals/README.md.",
            file=sys.stderr,
        )
    if regressions:
        print("\nREGRESSION against " + str(args.baseline) + ":", file=sys.stderr)
        for regression in regressions:
            print(f"  - {regression}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"\nno regression against {args.baseline} "
        f"(precision={score.precision:.4f} recall={score.recall:.4f} f1={score.f1:.4f})",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m service.eval_harness",
        description=(
            "Run the Diffuse review engine against committed fixtures and compare "
            "the scored result against a baseline."
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
    run.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse the cases already in --output instead of paying for them "
            "again. The file is rewritten after every fixture, so an interrupted "
            "run leaves one to resume from. A case whose fixture changed since "
            "is re-run."
        ),
    )
    run.set_defaults(handler=_run)

    capture = subparsers.add_parser(
        "capture",
        help="Turn a suite JSON into a baseline. Offline; no model calls.",
    )
    capture.add_argument("--suite", type=Path, required=True)
    capture.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    capture.add_argument(
        "--allow-zero-recall",
        action="store_true",
        help=(
            "Record a baseline that found none of its labeled defects. Such a "
            "baseline passes against every later run, so this has to be asked for "
            "explicitly."
        ),
    )
    capture.set_defaults(handler=_capture)

    check = subparsers.add_parser(
        "check",
        help="Score a suite JSON against a baseline and exit 1 on a regression",
    )
    check.add_argument("--suite", type=Path, required=True)
    check.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
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
