"""Self-hosted local-branch review CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlsplit, urlunsplit

# litellm builds its provider errors on the openai SDK hierarchy, so openai.OpenAIError
# is the only base that catches every model failure litellm can raise.
import openai
import psycopg2
from pydantic import BaseModel, ConfigDict, Field

from indexer.store import DEFAULT_DATABASE_URL, active_snapshot_id_for_repository, get_conn
from repository_policy.discovery import discover_repository_policy
from repository_policy.models import validate_repo_path
from repository_policy.resolve import (
    apply_approved_custom_contexts,
    apply_approved_learned_rules,
    resolve_review_policy,
)
from retriever.retrieve import parse_changed_files, retrieve_context_from_plan
from service import (
    cluster_cli,
    database_cli,
    evaluation_cli,
    learning_cli,
    model_cli,
    repository_cli,
    token_cli,
)
from service.cross_repository import resolve_cross_repository_context_plan
from service.custom_context_store import load_active_custom_contexts
from service.diff_parser import ParsedDiff, parse_unified_diff
from service.learning_store import load_active_learned_rules
from service.model_providers import resolve_provider
from service.repositories import RegisteredRepository, list_repositories
from service.review_engine import (
    PROMPT_VERSION,
    generate_review,
    resolve_review_depth_support,
    review_model,
    review_verifier_model,
)
from service.review_models import ReviewFinding, ReviewReport
from service.scm import validate_branch_name

MAX_LOCAL_DIFF_BYTES = 5 * 1024 * 1024
MAX_GIT_ERROR_CHARS = 2000
MAX_ERROR_LINES = 12
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

# Documented, stable exit codes. CI depends on these; keep them and the help
# epilog and README table in sync.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_INTERNAL = 4

# Names that must be redacted even when the suffix scan below would miss them.
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "DIFFUSE_API_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_WEBHOOK_SECRET",
    "POSTGRES_PASSWORD",
)

# A hand-maintained list silently rots: this one omitted every model provider
# except OpenAI while naming a DIFFUSE_WEBHOOK_SECRET that does not exist
# anywhere in the codebase. The suffix scan covers new providers, per-installation
# credentials, and anything an operator adds, while deliberately not matching
# non-secret configuration such as REVIEW_API_BASE or VERTEXAI_PROJECT, whose
# values are useful in a diagnostic.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PRIVATE_KEY")


def _secret_values() -> list[str]:
    """Every configured secret value, longest first.

    Longest first matters: when one secret contains another as a substring,
    replacing the shorter first would leave the tail of the longer one behind.
    """
    values: set[str] = set()
    for name, value in os.environ.items():
        if name in SECRET_ENV_NAMES or name.endswith(_SECRET_ENV_SUFFIXES):
            candidate = value.strip()
            # Short values are usually placeholders, and redacting them would
            # mangle unrelated text that happens to contain the same characters.
            if len(candidate) >= 8:
                values.add(candidate)
    return sorted(values, key=len, reverse=True)

EXIT_CODE_HELP = """\
exit codes:
  0  success (a clean review, or findings without --fail-on-findings)
  1  findings were reported and --fail-on-findings was supplied
  2  usage error: unknown flag, missing argument, or invalid argument value
  3  configuration or environment error: database, credentials, index, or policy
  4  internal error
"""

TOP_LEVEL_EPILOG = (
    """\
examples:
  diffuse repository list
  diffuse review -b origin/main --diff
  diffuse review --json

"""
    + EXIT_CODE_HELP
)

REVIEW_EPILOG = (
    """\
examples:
  diffuse review                          review this branch against its base
  diffuse review -b origin/main --diff    pick the base and show diff excerpts
  diffuse review --json                   emit diffuse-cli-review-v1 on stdout
  diffuse review --agent                  emit terminal-safe text for agents
  diffuse review --resume                 retry the last interrupted review
  diffuse review --fail-on-findings       CI gate: exit 1 when findings exist

Progress is written to stderr only when stderr is a terminal, so --json and
--agent stdout stays byte-for-byte stable in pipelines and CI logs.

"""
    + EXIT_CODE_HELP
)


class CliUsageError(ValueError):
    """An argument value supplied on the command line is invalid."""


@dataclass(frozen=True)
class LocalDiff:
    root: Path
    base_ref: str
    merge_base_sha: str
    head_sha: str
    diff_text: str
    untracked_paths: tuple[str, ...]
    included_untracked: bool = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.diff_text.encode()).hexdigest()


@dataclass(frozen=True)
class LocalReviewResult:
    repository: RegisteredRepository
    local_diff: LocalDiff
    snapshot_id: int
    review_model_name: str
    review_verifier_model_name: str
    prompt_version: str
    report: ReviewReport


class CliReviewState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["diffuse-cli-state-v2"] = "diffuse-cli-state-v2"
    status: Literal["running", "failed", "completed"]
    repository_id: int = Field(gt=0)
    repository_full_name: str = Field(min_length=3, max_length=512)
    base_ref: str = Field(min_length=1, max_length=255)
    diff_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    include_untracked: bool
    index_snapshot_id: int = Field(gt=0)
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_model: str = Field(min_length=1, max_length=512)
    # Both stages are part of the run's identity. Recording only the candidate
    # let `--resume` retry with a verifier the operator changed between
    # attempts, so the stored state no longer described which models produced
    # the result.
    review_verifier_model: str = Field(min_length=1, max_length=512)
    prompt_version: str = Field(min_length=1, max_length=255)
    attempt_count: int = Field(ge=1)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_]{1,64}$",
    )


def _git_bytes(
    root: Path,
    arguments: list[str],
    *,
    accepted_codes: tuple[int, ...] = (0,),
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode not in accepted_codes:
        message = result.stderr.decode(errors="replace").strip()
        detail = message[:MAX_GIT_ERROR_CHARS] or "unknown error"
        # Git writes multi-line diagnostics. Keep the newlines: the CLI error
        # formatter indents them instead of pasting them into one line.
        raise ValueError(
            f"git {' '.join(arguments)} failed with status {result.returncode}:\n{detail}"
        )
    return result.stdout


def find_repository_root(start: Path) -> Path:
    output = _git_bytes(start.resolve(), ["rev-parse", "--show-toplevel"])
    try:
        root = Path(output.decode().strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as error:
        raise ValueError("Git returned an invalid repository root") from error
    if not root.is_dir():
        raise ValueError("Git repository root is not a directory")
    return root


def _remote_parts(value: str) -> tuple[str, str]:
    value = value.strip()
    scp_match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", value)
    if scp_match and "://" not in value:
        host, path = scp_match.groups()
    else:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            raise ValueError("Origin remote is not a supported Git URL")
        host = parsed.hostname
        path = parsed.path
    normalized_path = path.strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    if (
        not normalized_path
        or any(part in {"", ".", ".."} for part in normalized_path.split("/"))
    ):
        raise ValueError("Origin remote has an invalid repository path")
    return host.casefold(), normalized_path


def select_registered_repository(
    root: Path,
    repositories: list[RegisteredRepository],
    *,
    requested_name: str | None = None,
    requested_base_url: str | None = None,
) -> RegisteredRepository:
    remote_host: str | None = None
    remote_path: str | None = None
    try:
        remote = _git_bytes(root, ["remote", "get-url", "origin"]).decode().strip()
        remote_host, remote_path = _remote_parts(remote)
    except (UnicodeDecodeError, ValueError):
        if requested_name is None:
            raise ValueError(
                "Cannot identify this checkout; configure origin or pass --repo"
            ) from None

    candidates = [repository for repository in repositories if repository.enabled]
    if requested_name is not None:
        candidates = [
            repository
            for repository in candidates
            if repository.full_name.casefold() == requested_name.casefold()
        ]
    if requested_base_url is not None:
        normalized_base = requested_base_url.rstrip("/").casefold()
        candidates = [
            repository
            for repository in candidates
            if repository.scm_base_url.casefold() == normalized_base
        ]
    if remote_host is not None and remote_path is not None:
        remote_matches = [
            repository
            for repository in candidates
            if urlsplit(repository.scm_base_url).hostname
            and urlsplit(repository.scm_base_url).hostname.casefold() == remote_host
            and (
                remote_path.casefold() == repository.full_name.casefold()
                or remote_path.casefold().endswith(
                    f"/{repository.full_name.casefold()}"
                )
            )
        ]
        if remote_matches or requested_name is None:
            candidates = remote_matches
    if len(candidates) != 1:
        if not candidates:
            raise ValueError(
                "This repository is not enabled in Diffuse; onboard and index it first"
            )
        raise ValueError(
            "Repository identity is ambiguous; pass --repo and --scm-base-url"
        )
    return candidates[0]


def _valid_base_ref(value: str) -> str:
    # validate_branch_name() is shared with SCM payload validation and reports the
    # `default_branch` field name. At the CLI boundary the value came from --base,
    # so re-word the failure without changing the shared validation semantics.
    try:
        return validate_branch_name(value)
    except ValueError as error:
        raise CliUsageError(
            f"--base is not a safe Git branch name: {value!r}"
        ) from error


def _revision_exists(root: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def resolve_base_ref(
    root: Path,
    repository: RegisteredRepository,
    requested_base: str | None,
) -> str:
    if requested_base is not None:
        candidate = _valid_base_ref(requested_base)
        if not _revision_exists(root, candidate):
            raise CliUsageError(
                f"--base revision does not exist in this checkout: {candidate}\n"
                "Fetch it first, for example with `git fetch origin`."
            )
        return candidate
    candidates = (
        f"origin/{repository.default_branch}",
        repository.default_branch,
    )
    for candidate in candidates:
        if _revision_exists(root, candidate):
            return candidate
    raise ValueError(
        f"Default base branch is unavailable locally: {repository.default_branch}"
    )


def _untracked_paths(root: Path) -> tuple[str, ...]:
    output = _git_bytes(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    paths: list[str] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            paths.append(validate_repo_path(raw.decode()))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("Untracked paths must be normalized UTF-8 paths") from error
    return tuple(sorted(paths))


def collect_local_diff(
    root: Path,
    repository: RegisteredRepository,
    *,
    requested_base: str | None = None,
    include_untracked: bool = False,
) -> LocalDiff:
    base_ref = resolve_base_ref(root, repository, requested_base)
    merge_base = _git_bytes(root, ["merge-base", "HEAD", base_ref]).decode().strip()
    head_sha = _git_bytes(root, ["rev-parse", "HEAD"]).decode().strip()
    diff = _git_bytes(
        root,
        [
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--find-renames",
            merge_base,
            "--",
        ],
    )
    if len(diff) > MAX_LOCAL_DIFF_BYTES:
        raise ValueError(
            f"Local diff exceeds the {MAX_LOCAL_DIFF_BYTES}-byte review limit"
        )
    untracked = _untracked_paths(root)
    if include_untracked:
        sections = [diff]
        total_bytes = len(diff)
        for path in untracked:
            file_path = root / path
            if file_path.is_symlink() or not file_path.is_file():
                raise ValueError(f"Untracked review input is not a regular file: {path}")
            if file_path.stat().st_size > MAX_LOCAL_DIFF_BYTES:
                raise ValueError(
                    f"Untracked review input exceeds the review limit: {path}"
                )
            section = _git_bytes(
                root,
                [
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--no-color",
                    "--",
                    "/dev/null",
                    path,
                ],
                accepted_codes=(0, 1),
            )
            total_bytes += len(section) + 1
            if total_bytes > MAX_LOCAL_DIFF_BYTES:
                raise ValueError(
                    f"Local diff exceeds the {MAX_LOCAL_DIFF_BYTES}-byte review limit"
                )
            sections.append(section)
        diff = b"\n".join(section for section in sections if section)
    try:
        diff_text = diff.decode()
    except UnicodeDecodeError as error:
        raise ValueError("Local diff must be UTF-8 text") from error
    return LocalDiff(
        root=root,
        base_ref=base_ref,
        merge_base_sha=merge_base,
        head_sha=head_sha,
        diff_text=diff_text,
        untracked_paths=untracked,
        included_untracked=include_untracked,
    )


def _state_path(root: Path) -> Path:
    raw = _git_bytes(root, ["rev-parse", "--git-common-dir"])
    try:
        common = Path(raw.decode().strip())
    except UnicodeDecodeError as error:
        raise ValueError("Git returned an invalid common directory") from error
    if not common.is_absolute():
        common = root / common
    return common.resolve() / "diffuse" / "cli-review.json"


def _load_state(root: Path) -> CliReviewState:
    path = _state_path(root)
    if path.is_symlink() or not path.is_file():
        raise ValueError("No resumable Diffuse review exists for this checkout")
    try:
        return CliReviewState.model_validate_json(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(
            "Stored Diffuse review state is invalid or was written by another "
            "Diffuse version; start a new review without --resume"
        ) from error


def _write_state(root: Path, state: CliReviewState) -> None:
    path = _state_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(state.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class ProgressReporter:
    """Single-line progress written to stderr so stdout contracts stay clean."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._width = 0
        self._model_steps = 0

    def stage(self, message: str) -> None:
        self._write(f"Diffuse: {message}...")

    def model_step(self) -> None:
        self._model_steps += 1
        self._write(f"Diffuse: running review model (step {self._model_steps})...")

    def clear(self) -> None:
        if self._width:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._width = 0

    def _write(self, text: str) -> None:
        padding = max(0, self._width - len(text))
        self._stream.write("\r" + text + " " * padding)
        self._stream.flush()
        self._width = len(text)


def _stderr_progress_reporter() -> ProgressReporter | None:
    """Report progress only on an interactive terminal; CI logs stay quiet."""
    stream = sys.stderr
    if stream is None or not hasattr(stream, "isatty") or not stream.isatty():
        return None
    return ProgressReporter(stream)


def report_review_depth(stream: TextIO | None = None) -> None:
    """Name what each model will actually be sent, and refuse the impossible.

    The worker resolves this at startup; the CLI has no startup, so it resolves
    it before the first model call instead. Written straight to stderr rather
    than through `ProgressReporter`, which overwrites its own line and stays
    silent off a terminal -- the opposite of what a diagnostic about a control
    the operator will not get needs to be.
    """

    support = resolve_review_depth_support()
    for line in support.report_lines():
        print(line, file=stream if stream is not None else sys.stderr)
    refusal = support.refusal()
    if refusal is not None:
        raise ValueError(refusal)


def run_local_review(
    *,
    start: Path,
    requested_repo: str | None = None,
    requested_base_url: str | None = None,
    requested_base: str | None = None,
    include_untracked: bool = False,
    resume: bool = False,
    progress: ProgressReporter | None = None,
) -> LocalReviewResult | None:
    def stage(message: str) -> None:
        if progress is not None:
            progress.stage(message)

    model_progress: Callable[[], None] | None = (
        progress.model_step if progress is not None else None
    )

    stage("resolving repository identity")
    root = find_repository_root(start)
    with closing(get_conn()) as conn:
        repository = select_registered_repository(
            root,
            list_repositories(conn),
            requested_name=requested_repo,
            requested_base_url=requested_base_url,
        )
    previous_state = _load_state(root) if resume else None
    if previous_state is not None:
        if previous_state.status == "completed":
            raise ValueError(
                "The latest local review completed; start a new review without --resume"
            )
        if previous_state.repository_id != repository.id:
            raise ValueError("Stored review belongs to another Diffuse repository")
        if requested_base is not None and requested_base != previous_state.base_ref:
            raise ValueError("--base cannot change while resuming a review")
        if include_untracked and not previous_state.include_untracked:
            raise ValueError("--include-untracked cannot change while resuming a review")
        requested_base = previous_state.base_ref
        include_untracked = previous_state.include_untracked
    stage("collecting the local diff")
    local_diff = collect_local_diff(
        root,
        repository,
        requested_base=requested_base,
        include_untracked=include_untracked,
    )
    if not local_diff.diff_text.strip():
        return None
    if (
        previous_state is not None
        and previous_state.diff_fingerprint != local_diff.fingerprint
    ):
        raise ValueError(
            "The local diff changed after the unfinished review; start a new review"
        )
    parsed = parse_unified_diff(local_diff.diff_text)
    changed_paths = {
        path
        for file in parsed.files
        for path in (file.old_path, file.new_path)
        if path is not None
    }
    if not changed_paths:
        changed_paths = parse_changed_files(local_diff.diff_text)
    stage("discovering repository policy")
    policy = resolve_review_policy(
        discover_repository_policy(root),
        changed_paths,
    )
    selected_review_model = review_model()
    selected_verifier_model = review_verifier_model()
    report_review_depth()
    with closing(get_conn()) as conn:
        snapshot_id = active_snapshot_id_for_repository(conn, repository.id)
        if snapshot_id is None:
            raise RuntimeError(
                "Repository has no compatible active index; run repository sync first"
            )
        learned_rules = load_active_learned_rules(
            conn,
            repository_id=repository.id,
        )
        custom_contexts = load_active_custom_contexts(
            conn,
            repository_id=repository.id,
        )
        policy = apply_approved_learned_rules(policy, learned_rules)
        policy = apply_approved_custom_contexts(policy, custom_contexts)
        context_plan = resolve_cross_repository_context_plan(
            conn,
            primary_repository_id=repository.id,
            primary_snapshot_id=snapshot_id,
            explicit_repositories=policy.context_repositories,
        )
    if previous_state is not None and (
        previous_state.index_snapshot_id != snapshot_id
        or previous_state.policy_fingerprint != policy.fingerprint
        or previous_state.review_model != selected_review_model
        or previous_state.review_verifier_model != selected_verifier_model
        or previous_state.prompt_version != PROMPT_VERSION
    ):
        raise ValueError(
            "Review inputs changed after the unfinished run; start a new review"
        )
    state = CliReviewState(
        status="running",
        repository_id=repository.id,
        repository_full_name=repository.full_name,
        base_ref=local_diff.base_ref,
        diff_fingerprint=local_diff.fingerprint,
        include_untracked=include_untracked,
        index_snapshot_id=snapshot_id,
        policy_fingerprint=policy.fingerprint,
        review_model=selected_review_model,
        review_verifier_model=selected_verifier_model,
        prompt_version=PROMPT_VERSION,
        attempt_count=(
            previous_state.attempt_count + 1
            if previous_state is not None
            else 1
        ),
    )
    _write_state(root, state)
    try:
        stage("retrieving repository context")
        context = retrieve_context_from_plan(local_diff.diff_text, context_plan)
        stage("running review model")
        report = generate_review(
            local_diff.diff_text,
            list(context.contexts),
            progress_callback=model_progress,
            policy=policy,
            # Pass the models recorded in the run state rather than letting
            # generation re-read the environment, so a resumed attempt uses the
            # pair the drift check just validated.
            candidate_model=selected_review_model,
            verifier_model=selected_verifier_model,
        )
    except Exception:
        _write_state(
            root,
            state.model_copy(
                update={
                    "status": "failed",
                    "error_code": "review_failed",
                }
            ),
        )
        raise
    _write_state(
        root,
        state.model_copy(
            update={
                "status": "completed",
                "error_code": None,
            }
        ),
    )
    return LocalReviewResult(
        repository=repository,
        local_diff=local_diff,
        snapshot_id=snapshot_id,
        review_model_name=selected_review_model,
        review_verifier_model_name=selected_verifier_model,
        prompt_version=PROMPT_VERSION,
        report=report,
    )


def _terminal_text(value: str) -> str:
    value = ANSI_ESCAPE_PATTERN.sub("", value)
    return "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32 and character != "\x7f"
    )


def _finding_text(
    finding: ReviewFinding,
    *,
    ordinal: int,
    snippet: str | None,
    agent: bool,
) -> str:
    prefix = "FINDING" if agent else "Finding"
    parts = [
        f"{prefix} {ordinal}: [{finding.severity.value.upper()}] "
        f"{finding.file_path}:{finding.line} — {finding.title}",
        finding.body,
        f"Evidence: {finding.evidence}",
        (
            f"Category: {finding.category.value}; "
            f"confidence: {finding.confidence:.0%}"
        ),
    ]
    if finding.suggested_fix:
        parts.append(f"Suggested fix: {finding.suggested_fix}")
    if snippet:
        parts.append(f"Relevant diff:\n{snippet}")
    return _terminal_text("\n".join(parts))


def render_human(result: LocalReviewResult, *, include_diff: bool = False) -> str:
    report = result.report
    metrics = (
        f"Risk {report.risk_score:.1f}/10 · Findings {len(report.findings)} · "
        f"Coverage {report.reviewed_file_count}/{report.diff_file_count}"
    )
    if report.confidence_score_section_included:
        metrics = f"Confidence {report.confidence_score}/5 · {metrics}"
    parts = [
        (
            f"Diffuse review: {result.repository.full_name} "
            f"({result.local_diff.head_sha[:12]} vs {result.local_diff.base_ref})"
        ),
        metrics,
    ]
    if report.summary_section_included:
        parts.extend(["", report.summary])
    if result.local_diff.untracked_paths and not result.local_diff.included_untracked:
        parts.extend(
            [
                "",
                (
                    f"Note: {len(result.local_diff.untracked_paths)} untracked "
                    "file(s) were excluded; pass --include-untracked to review them."
                ),
            ]
        )
    if report.diagram is not None:
        parts.extend(
            [
                "",
                f"Change diagram ({report.diagram.kind.value}): {report.diagram.title}",
                report.diagram.mermaid,
            ]
        )
    parsed = parse_unified_diff(result.local_diff.diff_text)
    for index, finding in enumerate(report.findings, start=1):
        snippet = (
            parsed.snippet(finding.file_path, finding.side, finding.line)
            if include_diff
            else None
        )
        parts.extend(
            [
                "",
                _finding_text(
                    finding,
                    ordinal=index,
                    snippet=snippet,
                    agent=False,
                ),
            ]
        )
    return _terminal_text("\n".join(parts)).rstrip() + "\n"


def render_agent(result: LocalReviewResult) -> str:
    report = result.report
    parsed = parse_unified_diff(result.local_diff.diff_text)
    parts = [
        "DIFFUSE REVIEW",
        f"Repository: {result.repository.full_name}",
        f"Base: {result.local_diff.base_ref}",
        f"Head: {result.local_diff.head_sha}",
        f"Confidence: {report.confidence_score}/5",
        f"Risk: {report.risk_score:.1f}/10",
        f"Summary: {report.summary}",
    ]
    if result.local_diff.untracked_paths and not result.local_diff.included_untracked:
        parts.append(
            f"Untracked files excluded: {len(result.local_diff.untracked_paths)}"
        )
    for index, finding in enumerate(report.findings, start=1):
        parts.extend(
            [
                "",
                _finding_text(
                    finding,
                    ordinal=index,
                    snippet=parsed.snippet(
                        finding.file_path,
                        finding.side,
                        finding.line,
                    ),
                    agent=True,
                ),
            ]
        )
    return _terminal_text("\n".join(parts)).rstrip() + "\n"


def render_json(result: LocalReviewResult, *, include_diff: bool = False) -> str:
    payload = {
        "schema_version": "diffuse-cli-review-v1",
        "repository": result.repository.full_name,
        "base_ref": result.local_diff.base_ref,
        "merge_base_sha": result.local_diff.merge_base_sha,
        "head_sha": result.local_diff.head_sha,
        "diff_fingerprint": result.local_diff.fingerprint,
        "untracked_paths": result.local_diff.untracked_paths,
        "included_untracked": result.local_diff.included_untracked,
        "index_snapshot_id": result.snapshot_id,
        "review_model": result.review_model_name,
        "review_verifier_model": result.review_verifier_model_name,
        "prompt_version": result.prompt_version,
        "report": result.report.model_dump(mode="json"),
    }
    if include_diff:
        parsed: ParsedDiff = parse_unified_diff(result.local_diff.diff_text)
        payload["diff_snippets"] = {
            finding.fingerprint: parsed.snippet(
                finding.file_path,
                finding.side,
                finding.line,
            )
            for finding in result.report.findings
        }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def _run_review_command(args: argparse.Namespace) -> None:
    progress = _stderr_progress_reporter()
    try:
        result = run_local_review(
            start=Path.cwd(),
            requested_repo=args.repo,
            requested_base_url=args.scm_base_url,
            requested_base=args.base,
            include_untracked=args.include_untracked,
            resume=args.resume,
            progress=progress,
        )
    finally:
        if progress is not None:
            progress.clear()
    if result is None:
        output = (
            json.dumps(
                {
                    "schema_version": "diffuse-cli-review-v1",
                    "status": "no_changes",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
            if args.json
            else "No changes to review.\n"
        )
    elif args.json:
        output = render_json(result, include_diff=args.diff)
    elif args.agent:
        output = render_agent(result)
    else:
        output = render_human(result, include_diff=args.diff)
    sys.stdout.write(output)
    if result is not None and args.fail_on_findings and result.report.findings:
        raise SystemExit(EXIT_FINDINGS)


def _parser() -> argparse.ArgumentParser:
    return _build_parser()[0]


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="diffuse",
        description="Self-hosted code intelligence and review",
        epilog=TOP_LEVEL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    review = subparsers.add_parser(
        "review",
        help="Review the current local branch against its base",
        description=(
            "Review committed, staged, and unstaged changes in this checkout against the\n"
            "merge base with its base branch, using the same index, policy, learned rules,\n"
            "retrieval, and verifier as the hosted service. The checkout must correspond to\n"
            "an enabled, indexed Diffuse repository."
        ),
        epilog=REVIEW_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review.add_argument(
        "-b",
        "--base",
        metavar="REF",
        help=(
            "Base revision to diff against (default: origin/<default branch> of the "
            "registered repository, falling back to <default branch>)"
        ),
    )
    review.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help=(
            "Registered repository full name; required when this checkout has no "
            "usable origin remote or matches more than one registered repository"
        ),
    )
    review.add_argument(
        "--scm-base-url",
        metavar="URL",
        help=(
            "SCM base URL, for example https://github.com, that disambiguates --repo "
            "when the same full name is registered on several hosts"
        ),
    )
    review.add_argument(
        "--include-untracked",
        action="store_true",
        help=(
            "Also review untracked files (default: untracked files are reported in "
            "the output but excluded from the diff)"
        ),
    )
    review.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Retry the last interrupted review for this checkout; refuses to run when "
            "the repository, diff, base, untracked choice, index snapshot, policy, "
            "model, or prompt version changed"
        ),
    )
    review.add_argument(
        "--diff",
        action="store_true",
        help="Include the exact diff excerpt for each finding (default: omitted)",
    )
    output = review.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the versioned diffuse-cli-review-v1 JSON document on stdout "
            "(default: human-readable text)"
        ),
    )
    output.add_argument(
        "--agent",
        action="store_true",
        help=(
            "Emit terminal-safe plain text with every finding, evidence item, and "
            "suggested fix, for consumption by a coding agent"
        ),
    )
    review.add_argument(
        "--fail-on-findings",
        action="store_true",
        help=(
            "Exit 1 when the review reports at least one finding, for CI gating "
            "(default: exit 0 whenever the review completes)"
        ),
    )
    review.set_defaults(handler=_run_review_command)

    repository = subparsers.add_parser(
        "repository",
        help="Onboard and manage indexed repositories",
    )
    repository_cli.configure_parser(repository)

    cluster = subparsers.add_parser(
        "cluster",
        help="Manage cross-repository context clusters",
    )
    cluster_cli.configure_parser(cluster)

    learning = subparsers.add_parser(
        "learning",
        help="Inspect and moderate feedback-derived rules",
    )
    learning_cli.configure_parser(learning)

    token = subparsers.add_parser(
        "token",
        help="Create, inspect, and revoke scoped service tokens",
    )
    token_cli.configure_parser(token)

    database = subparsers.add_parser(
        "database",
        help="Inspect, migrate, and verify the PostgreSQL schema",
    )
    database_cli.configure_parser(database)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Score a labeled review-quality evaluation set",
    )
    evaluation_cli.configure_parser(evaluate)

    model = subparsers.add_parser(
        "model",
        help="Inspect or verify the configured review model",
    )
    model_cli.configure_parser(model)

    # Every subparser must appear here, or an unknown flag typed on that
    # subcommand is reported against the top-level parser and prints the wrong
    # usage block -- the defect this mapping exists to fix.
    commands = {
        "review": review,
        "repository": repository,
        "cluster": cluster,
        "learning": learning,
        "token": token,
        "database": database,
        "evaluate": evaluate,
        "model": model,
    }
    return parser, commands


def redact_secrets(text: str) -> str:
    """Remove any configured secret value that leaked into a diagnostic string."""
    for value in _secret_values():
        text = text.replace(value, "***")
    password = _database_password()
    if password:
        text = text.replace(password, "***")
    return text


def _database_password() -> str | None:
    try:
        return urlsplit(_database_url()).password
    except ValueError:
        return None


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL


def redacted_database_url() -> str:
    """The effective connection URL with any password replaced by ``***``."""
    raw = _database_url()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "<unparseable DATABASE_URL>"
    if not parts.password or not parts.hostname:
        return raw
    userinfo = f"{parts.username}:***@" if parts.username else "***@"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=f"{userinfo}{parts.hostname}{port}"))


def database_error_message(error: psycopg2.Error) -> str:
    configured = bool(os.environ.get("DATABASE_URL", "").strip())
    source = "DATABASE_URL" if configured else "the built-in default"
    detail = str(error).strip() or error.__class__.__name__
    if isinstance(error, psycopg2.OperationalError):
        return (
            f"Cannot connect to PostgreSQL at {redacted_database_url()} (from {source}).\n"
            f"{detail}\n"
            "Start the database with `docker compose up -d db`, or set DATABASE_URL to a "
            "reachable instance."
        )
    return (
        f"PostgreSQL rejected a Diffuse query on {redacted_database_url()} (from {source}); "
        "the schema may be missing or out of date.\n"
        f"{detail}\n"
        "Apply migrations with `docker compose run --rm migrate database migrate`."
    )


def model_error_message(error: openai.OpenAIError) -> str:
    try:
        model = review_model()
    except ValueError:
        model = os.environ.get("REVIEW_MODEL", "").strip() or "<unset>"
    detail = str(error).strip() or error.__class__.__name__
    if isinstance(error, openai.AuthenticationError | openai.PermissionDeniedError):
        # Name the credential this model actually authenticates with. litellm raises
        # the openai exception hierarchy for every provider, so hardcoding
        # OPENAI_API_KEY here sent anyone who typo'd an Anthropic key -- the default
        # provider -- to fix a variable that has nothing to do with the failure.
        credentials = " or ".join(resolve_provider(model).credential_env_names)
        head = (
            f"The review model provider rejected the credential for REVIEW_MODEL={model}.\n"
            f"{detail}\n"
            f"Set {credentials} to a key valid for that model."
        )
    elif isinstance(error, openai.APIConnectionError):
        head = (
            f"Cannot reach the review model endpoint for REVIEW_MODEL={model}.\n"
            f"{detail}\n"
            "Check network access and REVIEW_API_BASE, then retry with `diffuse review --resume`."
        )
    elif isinstance(error, openai.RateLimitError):
        head = (
            f"The review model provider rate-limited REVIEW_MODEL={model}.\n"
            f"{detail}\n"
            "Wait and retry with `diffuse review --resume`."
        )
    else:
        head = (
            f"The review model request failed for REVIEW_MODEL={model}.\n"
            f"{detail}\n"
            "Verify REVIEW_MODEL and REVIEW_API_BASE, then retry with "
            "`diffuse review --resume`."
        )
    return head


def format_cli_error(message: str) -> str:
    """Render a possibly multi-line message as a readable, usage-free CLI error."""
    lines = [line.rstrip() for line in redact_secrets(message).strip().splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        lines = ["unknown error"]
    rendered = [f"diffuse: error: {lines[0]}"]
    rendered.extend(f"    {line.strip()}" for line in lines[1:MAX_ERROR_LINES])
    suppressed = len(lines) - MAX_ERROR_LINES
    if suppressed > 0:
        rendered.append(f"    ... {suppressed} more line(s) suppressed")
    return "\n".join(rendered) + "\n"


def _fail(message: str, code: int) -> None:
    sys.stderr.write(format_cli_error(message))
    raise SystemExit(code)


def run_handler(args: argparse.Namespace) -> None:
    """Invoke the selected handler, mapping every failure to a documented exit code."""
    debug = bool(os.environ.get("DIFFUSE_CLI_TRACEBACK", "").strip())
    try:
        args.handler(args)
    except SystemExit:
        raise
    except CliUsageError as error:
        if debug:
            raise
        _fail(str(error), EXIT_USAGE)
    except psycopg2.Error as error:
        if debug:
            raise
        _fail(database_error_message(error), EXIT_CONFIG)
    except openai.OpenAIError as error:
        if debug:
            raise
        _fail(model_error_message(error), EXIT_CONFIG)
    except (OSError, RuntimeError, ValueError) as error:
        if debug:
            raise
        _fail(str(error), EXIT_CONFIG)
    except Exception as error:
        if debug:
            raise
        _fail(
            f"Internal error: {error.__class__.__name__}: {error}\n"
            "Re-run with DIFFUSE_CLI_TRACEBACK=1 for the full traceback, and report this "
            "as a Diffuse bug.",
            EXIT_INTERNAL,
        )


def _install_redacting_excepthook() -> None:
    """Redact secrets from an unhandled traceback.

    ``DIFFUSE_CLI_TRACEBACK=1`` re-raises the original exception, and the README
    advertises that as the way to file a bug report -- so its output is exactly
    what an operator pastes into a ticket. Python's default handler would print
    it verbatim, and ``psycopg2.OperationalError`` routinely embeds the whole
    connection string, password included.
    """
    original = sys.excepthook

    def hook(exc_type, exc_value, exc_traceback):
        rendered = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        sys.stderr.write(redact_secrets(rendered))

    sys.excepthook = hook
    return original


def main() -> None:
    if os.environ.get("DIFFUSE_CLI_TRACEBACK", "").strip():
        _install_redacting_excepthook()
    parser, commands = _build_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        # argparse reports leftovers against the top-level parser, which prints an
        # irrelevant usage block for a subcommand's flag. Report them where they
        # were typed instead. ArgumentParser.error() exits with EXIT_USAGE.
        target = commands.get(getattr(args, "command", ""), parser)
        target.error(redact_secrets(f"unrecognized arguments: {' '.join(unknown)}"))
    run_handler(args)


if __name__ == "__main__":
    main()
