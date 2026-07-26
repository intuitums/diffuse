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
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from indexer.embed import embedding_dimensions, embedding_model
from indexer.store import active_snapshot_id_for_repository, get_conn
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
from service.repositories import RegisteredRepository, list_repositories
from service.review_engine import (
    PROMPT_VERSION,
    generate_review,
    review_model,
    review_verifier_model,
)
from service.review_models import ReviewFinding, ReviewReport
from service.scm import validate_branch_name

MAX_LOCAL_DIFF_BYTES = 5 * 1024 * 1024
MAX_GIT_ERROR_CHARS = 2000
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


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
        raise ValueError(
            f"Git command failed: {message[:MAX_GIT_ERROR_CHARS] or 'unknown error'}"
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
    return validate_branch_name(value)


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
            raise ValueError(f"Base revision does not exist: {candidate}")
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


def run_local_review(
    *,
    start: Path,
    requested_repo: str | None = None,
    requested_base_url: str | None = None,
    requested_base: str | None = None,
    include_untracked: bool = False,
    resume: bool = False,
) -> LocalReviewResult | None:
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
    policy = resolve_review_policy(
        discover_repository_policy(root),
        changed_paths,
    )
    model = embedding_model()
    dimensions = embedding_dimensions()
    selected_review_model = review_model()
    selected_verifier_model = review_verifier_model()
    with closing(get_conn()) as conn:
        snapshot_id = active_snapshot_id_for_repository(
            conn,
            repository.id,
            model,
            dimensions,
        )
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
            model=model,
            dimensions=dimensions,
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
        context = retrieve_context_from_plan(local_diff.diff_text, context_plan)
        report = generate_review(
            local_diff.diff_text,
            list(context.contexts),
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
    result = run_local_review(
        start=Path.cwd(),
        requested_repo=args.repo,
        requested_base_url=args.scm_base_url,
        requested_base=args.base,
        include_untracked=args.include_untracked,
        resume=args.resume,
    )
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
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffuse",
        description="Self-hosted code intelligence and review",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    review = subparsers.add_parser(
        "review",
        help="Review the current local branch against its base",
    )
    review.add_argument("-b", "--base")
    review.add_argument("--repo")
    review.add_argument("--scm-base-url")
    review.add_argument("--include-untracked", action="store_true")
    review.add_argument("--resume", action="store_true")
    review.add_argument("--diff", action="store_true")
    output = review.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--agent", action="store_true")
    review.add_argument("--fail-on-findings", action="store_true")
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
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
