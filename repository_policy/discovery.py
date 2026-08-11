"""Discover bounded, tracked repository review policy from a clean checkout."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .models import (
    ContextFilesConfig,
    GuidanceDocument,
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
    is_sensitive_repo_path,
    validate_repo_glob,
    validate_repo_path,
)

LOGGER = logging.getLogger(__name__)

MAX_POLICY_SOURCE_BYTES = 128 * 1024
MAX_CONTEXT_FILE_BYTES = 128 * 1024
MAX_TOTAL_POLICY_BYTES = 1024 * 1024
COMMON_INSTRUCTION_NAMES = frozenset(
    {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".cursorrules"}
)


def _git_tracked_paths(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise ValueError(f"Unable to enumerate tracked repository files: {message}")
    paths = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            path = validate_repo_path(raw_path.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("Tracked policy paths must be normalized UTF-8 paths") from error
        paths.append(path)
    return tuple(sorted(paths))


def _read_text(
    root: Path,
    path: str,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    file_path = root / path
    if file_path.is_symlink() or not file_path.is_file():
        raise ValueError(f"Policy source must be a tracked regular file: {path}")
    content = file_path.read_bytes()
    if len(content) > max_bytes:
        raise ValueError(f"Policy source exceeds its size limit: {path}")
    try:
        return content.decode("utf-8"), len(content)
    except UnicodeDecodeError as error:
        raise ValueError(f"Policy source must be UTF-8 text: {path}") from error


def _skip_irregular_convention_file(root: Path, source_path: str) -> bool:
    # Repositories commonly track e.g. a CLAUDE.md -> AGENTS.md symlink. Guidance
    # picked up by filename convention is skipped rather than failing discovery;
    # symlinks are never read. Explicit references (.diffuse/ files) still fail.
    file_path = root / source_path
    if file_path.is_symlink() or not file_path.is_file():
        LOGGER.warning("Skipped non-regular guidance file %s", source_path)
        return True
    return False


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _parse_json_model(model_type, content: str, source_path: str):
    try:
        raw = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
        return model_type.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ValueError(f"Invalid repository policy in {source_path}: {error}") from error


def _directory_for_diffuse_file(path: PurePosixPath) -> str:
    directory = path.parent.parent.as_posix()
    return "" if directory == "." else directory


def _directory_for_common_instruction(path: PurePosixPath) -> str:
    if path.name == "copilot-instructions.md" and path.parent.name == ".github":
        directory = path.parent.parent
    elif (
        path.suffix == ".mdc"
        and path.parent.name == "rules"
        and path.parent.parent.name == ".cursor"
    ):
        directory = path.parent.parent.parent
    else:
        directory = path.parent
    value = directory.as_posix()
    return "" if value == "." else value


def _is_common_instruction(path: PurePosixPath) -> bool:
    if path.name in COMMON_INSTRUCTION_NAMES:
        return True
    if path.name == "copilot-instructions.md" and path.parent.name == ".github":
        return True
    return (
        path.suffix == ".mdc"
        and path.parent.name == "rules"
        and path.parent.parent.name == ".cursor"
    )


def _cursor_globs(content: str) -> tuple[str, ...]:
    """Read common one-line Cursor MDC globs without accepting YAML features."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return ("**",)
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return ("**",)
    for line in lines[1:end]:
        key, separator, raw_value = line.partition(":")
        if separator and key.strip() == "globs":
            raw_value = raw_value.strip()
            if raw_value.startswith("[") and raw_value.endswith("]"):
                raw_value = raw_value[1:-1]
            values = tuple(
                validate_repo_glob(item.strip().strip("\"'"))
                for item in raw_value.split(",")
                if item.strip().strip("\"'")
            )
            return values or ("**",)
    return ("**",)


def _join_relative(directory: str, path: str) -> str:
    joined = f"{directory}/{path}" if directory else path
    return validate_repo_path(PurePosixPath(joined).as_posix())


def _validate_rule_overrides(layers: list[PolicyLayer]) -> None:
    known_by_directory: dict[str, set[str]] = defaultdict(set)
    for layer in sorted(
        layers,
        key=lambda item: (item.directory_path.count("/"), item.source_path),
    ):
        ancestors = {
            rule_id
            for directory, rule_ids in known_by_directory.items()
            if not directory
            or layer.directory_path == directory
            or layer.directory_path.startswith(f"{directory}/")
            for rule_id in rule_ids
        }
        local_ids = {rule.id for rule in layer.config.rules}
        unknown = set(layer.config.rule_overrides) - ancestors - local_ids
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise ValueError(f"{layer.source_path} overrides unknown rule IDs: {rendered}")
        known_by_directory[layer.directory_path].update(local_ids)


def discover_repository_policy(root: Path) -> RepositoryPolicySnapshot:
    root = root.resolve()
    tracked_paths = _git_tracked_paths(root)
    tracked = set(tracked_paths)
    layers: list[PolicyLayer] = []
    guidance: list[GuidanceDocument] = []
    total_bytes = 0

    for source_path in tracked_paths:
        path = PurePosixPath(source_path)
        if path.name == "config.json" and path.parent.name == ".diffuse":
            content, size = _read_text(
                root,
                source_path,
                max_bytes=MAX_POLICY_SOURCE_BYTES,
            )
            total_bytes += size
            config = _parse_json_model(RepositoryConfig, content, source_path)
            layers.append(
                PolicyLayer(
                    directory_path=_directory_for_diffuse_file(path),
                    source_path=source_path,
                    config=config,
                )
            )
        elif path.name == "rules.md" and path.parent.name == ".diffuse":
            if _skip_irregular_convention_file(root, source_path):
                continue
            content, size = _read_text(
                root,
                source_path,
                max_bytes=MAX_POLICY_SOURCE_BYTES,
            )
            total_bytes += size
            guidance.append(
                GuidanceDocument(
                    directory_path=_directory_for_diffuse_file(path),
                    source_path=source_path,
                    kind="rules",
                    applies_to=("**",),
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    priority=20,
                )
            )
        elif _is_common_instruction(path):
            if _skip_irregular_convention_file(root, source_path):
                continue
            content, size = _read_text(
                root,
                source_path,
                max_bytes=MAX_POLICY_SOURCE_BYTES,
            )
            total_bytes += size
            guidance.append(
                GuidanceDocument(
                    directory_path=_directory_for_common_instruction(path),
                    source_path=source_path,
                    kind="instructions",
                    applies_to=_cursor_globs(content) if path.suffix == ".mdc" else ("**",),
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    priority=10,
                )
            )

    referenced_contexts: set[tuple[str, str, tuple[str, ...]]] = set()
    for source_path in tracked_paths:
        path = PurePosixPath(source_path)
        if path.name != "files.json" or path.parent.name != ".diffuse":
            continue
        content, size = _read_text(
            root,
            source_path,
            max_bytes=MAX_POLICY_SOURCE_BYTES,
        )
        total_bytes += size
        config = _parse_json_model(ContextFilesConfig, content, source_path)
        directory = _directory_for_diffuse_file(path)
        for entry in config.files:
            context_path = _join_relative(directory, entry.path)
            if context_path not in tracked:
                raise ValueError(
                    f"{source_path} references an untracked or missing file: {context_path}"
                )
            # A repository must not be able to exfiltrate its own secrets to the model
            # provider by naming a secret-shaped file as review context.
            if is_sensitive_repo_path(context_path):
                LOGGER.warning(
                    "Dropped secret-shaped context file %s referenced by %s",
                    context_path,
                    source_path,
                )
                continue
            identity = (directory, context_path, entry.applies_to)
            if identity in referenced_contexts:
                raise ValueError(f"Duplicate context-file reference in {source_path}: {entry.path}")
            referenced_contexts.add(identity)
            context, context_size = _read_text(
                root,
                context_path,
                max_bytes=MAX_CONTEXT_FILE_BYTES,
            )
            total_bytes += context_size
            guidance.append(
                GuidanceDocument(
                    directory_path=directory,
                    source_path=context_path,
                    kind="context",
                    applies_to=entry.applies_to,
                    content=context,
                    content_hash=hashlib.sha256(context.encode()).hexdigest(),
                    description=entry.description,
                    priority=0,
                )
            )

    if total_bytes > MAX_TOTAL_POLICY_BYTES:
        raise ValueError("Repository policy exceeds the 1 MiB aggregate size limit")

    layers.sort(
        key=lambda item: (
            item.directory_path.count("/"),
            item.directory_path,
            item.source_path,
        )
    )
    guidance.sort(
        key=lambda item: (
            item.directory_path.count("/"),
            item.directory_path,
            -item.priority,
            item.source_path,
            item.applies_to,
        )
    )
    _validate_rule_overrides(layers)
    return RepositoryPolicySnapshot(
        layers=tuple(layers),
        guidance_documents=tuple(guidance),
    )
