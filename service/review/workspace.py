"""Bounded, immutable source artifacts for isolated agent workspaces.

The agent runner must never clone a repository or receive SCM credentials.  The
control plane instead supplies a snapshot as a plain tar archive and this
module is the deliberately small boundary which turns that archive into the
runner's read-only directory.  It does *not* use :meth:`TarFile.extractall`:
archive names and member types are validated before any filesystem write.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Source delivery is deliberately an in-memory signed envelope in the pilot.
# A 16 MiB tar expands to roughly 29 MiB after its two base64 encodings, and
# validation then makes a few short-lived copies.  Keep that bounded well below
# the runner's 1 GiB container limit; larger repositories remain on the
# transitional runtime until artifact transport is object-backed.
DEFAULT_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_WORKSPACE_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_WORKSPACE_ENTRIES = 20_000
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PATH_BYTES = 4_096
_COPY_CHUNK_BYTES = 128 * 1024
_MANIFEST_VERSION = 1


class SourceArtifactError(ValueError):
    """An artifact cannot safely become an isolated review workspace."""


@dataclass(frozen=True)
class WorkspaceLimits:
    """Explicit upper bounds for one source artifact and its extraction."""

    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES
    max_entries: int = DEFAULT_MAX_WORKSPACE_ENTRIES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_path_bytes: int = DEFAULT_MAX_PATH_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_archive_bytes",
            "max_workspace_bytes",
            "max_entries",
            "max_file_bytes",
            "max_path_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_file_bytes > self.max_workspace_bytes:
            raise ValueError("max_file_bytes must not exceed max_workspace_bytes")


DEFAULT_WORKSPACE_LIMITS = WorkspaceLimits()


@dataclass(frozen=True)
class SourceArtifact:
    """An opaque, immutable tar payload supplied by the control plane."""

    archive: bytes
    #: The canonical extracted-tree identity, when the control plane computed
    #: one. Dispatch binds this separately from the raw archive digest so the
    #: runner can refuse a format/parser disagreement before it starts a CLI.
    manifest_digest: str | None = None
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.archive, bytes):
            raise TypeError("source artifact archive must be bytes")
        if self.manifest_digest is not None and not (
            isinstance(self.manifest_digest, str)
            and len(self.manifest_digest) == 64
            and all(character in "0123456789abcdef" for character in self.manifest_digest)
        ):
            raise ValueError("source artifact manifest_digest must be a lowercase SHA-256 value")
        object.__setattr__(self, "digest", hashlib.sha256(self.archive).hexdigest())

    @property
    def archive_bytes(self) -> bytes:
        """A spelling that makes the payload type clear at dispatch call sites."""

        return self.archive


@dataclass(frozen=True)
class WorkspaceEntry:
    """One canonical filesystem member covered by a workspace manifest."""

    path: str
    kind: str
    mode: int
    size: int = 0
    sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "kind": self.kind,
            "mode": self.mode,
            "size": self.size,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        return result


@dataclass(frozen=True)
class WorkspaceManifest:
    """Canonical content identity of the extracted, read-only workspace."""

    entries: tuple[WorkspaceEntry, ...]
    file_count: int
    total_bytes: int
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        expected_file_count = sum(entry.kind == "file" for entry in self.entries)
        expected_total_bytes = sum(entry.size for entry in self.entries if entry.kind == "file")
        if self.file_count != expected_file_count or self.total_bytes != expected_total_bytes:
            raise ValueError("workspace manifest totals do not match its entries")
        if tuple(sorted(self.entries, key=lambda entry: entry.path)) != self.entries:
            raise ValueError("workspace manifest entries must be path-sorted")
        encoded = self.canonical_json().encode("utf-8")
        object.__setattr__(self, "digest", hashlib.sha256(encoded).hexdigest())

    def as_dict(self) -> dict[str, object]:
        return {
            "version": _MANIFEST_VERSION,
            "entries": [entry.as_dict() for entry in self.entries],
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }

    def canonical_json(self) -> str:
        """Stable JSON that may be bound into a later signed dispatch."""

        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class _ValidatedFile:
    path: str
    size: int
    sha256: str
    executable: bool


@dataclass(frozen=True)
class _ValidatedArchive:
    files: tuple[_ValidatedFile, ...]
    directories: frozenset[str]
    manifest: WorkspaceManifest


def build_source_artifact(
    source_root: Path,
    *,
    limits: WorkspaceLimits = DEFAULT_WORKSPACE_LIMITS,
) -> SourceArtifact:
    """Build a deterministic, bounded tar snapshot from a real source directory.

    Symlinks and special files are deliberately not represented.  A checkout is
    expected to be ordinary files and directories; silently resolving a link
    here could archive content outside the selected revision.
    """

    root = Path(source_root)
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise SourceArtifactError("source root is unavailable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise SourceArtifactError("source root must be a real directory")

    output = io.BytesIO()
    state = _BuildState(limits=limits)
    try:
        root_descriptor = _open_source_directory(root, root_metadata)
        try:
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                _add_source_directory(archive, root_descriptor, PurePosixPath(), state)
                state.check_archive_size(output.tell())
        finally:
            os.close(root_descriptor)
    except (OSError, tarfile.TarError) as error:
        raise SourceArtifactError("source root could not be archived safely") from error
    payload = output.getvalue()
    if len(payload) > limits.max_archive_bytes:
        raise SourceArtifactError("source artifact exceeds max_archive_bytes")
    # Bind the canonical extracted tree as well as the exact transport bytes.
    # This second pass is intentionally before dispatch: runner validation must
    # agree with the control plane before a credential-bearing CLI sees source.
    manifest = _validate_archive(payload, limits).manifest
    return SourceArtifact(payload, manifest_digest=manifest.digest)


@dataclass
class _BuildState:
    limits: WorkspaceLimits
    entries: int = 0
    total_bytes: int = 0

    def add_entry(self, path: str) -> None:
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise SourceArtifactError("source artifact exceeds max_entries")
        _canonical_member_path(path, self.limits)

    def add_file(self, size: int) -> None:
        if size < 0 or size > self.limits.max_file_bytes:
            raise SourceArtifactError("source file exceeds max_file_bytes")
        self.total_bytes += size
        if self.total_bytes > self.limits.max_workspace_bytes:
            raise SourceArtifactError("source artifact exceeds max_workspace_bytes")

    def check_archive_size(self, size: int) -> None:
        if size > self.limits.max_archive_bytes:
            raise SourceArtifactError("source artifact exceeds max_archive_bytes")


def _add_source_directory(
    archive: tarfile.TarFile,
    directory_descriptor: int,
    relative: PurePosixPath,
    state: _BuildState,
) -> None:
    try:
        with os.scandir(directory_descriptor) as scanner:
            children = sorted(scanner, key=lambda entry: entry.name.encode("utf-8"))
    except (OSError, UnicodeError) as error:
        raise SourceArtifactError("source directory could not be read safely") from error
    for child in children:
        # `git worktree add` writes this control file at the checkout root. It
        # names a private control-plane path and is not source at the reviewed
        # revision, so never deliver it to the runner. A nested `.git` path is
        # ordinary source data and remains subject to the regular file rules.
        if not relative.parts and child.name == ".git":
            continue
        path = relative / child.name
        canonical_path = _canonical_member_path(path.as_posix(), state.limits)
        try:
            metadata = child.stat(follow_symlinks=False)
        except OSError as error:
            raise SourceArtifactError(f"source member is unavailable: {canonical_path}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SourceArtifactError(f"source member must not be a symlink: {canonical_path}")
        if stat.S_ISDIR(metadata.st_mode):
            state.add_entry(canonical_path)
            _add_directory_member(archive, canonical_path)
            state.check_archive_size(archive.fileobj.tell())
            child_descriptor = _open_source_directory_at(
                directory_descriptor,
                child.name,
                metadata,
                canonical_path,
            )
            try:
                _add_source_directory(archive, child_descriptor, path, state)
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(metadata.st_mode):
            state.add_entry(canonical_path)
            state.add_file(metadata.st_size)
            _add_file_member(
                archive,
                directory_descriptor,
                child.name,
                canonical_path,
                metadata,
            )
            state.check_archive_size(archive.fileobj.tell())
        else:
            raise SourceArtifactError(
                f"source member must be a regular file or directory: {canonical_path}"
            )


def _add_directory_member(archive: tarfile.TarFile, path: str) -> None:
    member = tarfile.TarInfo(f"{path}/")
    member.type = tarfile.DIRTYPE
    member.mode = 0o555
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    archive.addfile(member)


def _add_file_member(
    archive: tarfile.TarFile,
    parent_descriptor: int,
    source_name: str,
    path: str,
    metadata: os.stat_result,
) -> None:
    member = tarfile.TarInfo(path)
    member.size = metadata.st_size
    member.mode = 0o555 if metadata.st_mode & 0o111 else 0o444
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source_name, flags, dir_fd=parent_descriptor)
        with os.fdopen(descriptor, "rb") as source:
            # Check the opened object too.  The first lstat prevents normal
            # links; comparing identity closes the replacement race before tar
            # reads, and the descriptor prevents a renamed parent directory
            # from redirecting this open outside the selected checkout.
            opened_metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_size != member.size
                or opened_metadata.st_dev != metadata.st_dev
                or opened_metadata.st_ino != metadata.st_ino
            ):
                raise SourceArtifactError(f"source member changed while archiving: {path}")
            archive.addfile(member, source)
    except OSError as error:
        raise SourceArtifactError(f"source member could not be read: {path}") from error


def _open_source_directory(root: Path, expected: os.stat_result) -> int:
    """Open the checked root without following a replacement symlink."""

    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise SourceArtifactError("source root could not be opened safely") from error
    try:
        _verify_directory_identity(os.fstat(descriptor), expected, str(root))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_source_directory_at(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
    path: str,
) -> int:
    """Open a child relative to its already-open parent directory."""

    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise SourceArtifactError(f"source member could not be opened: {path}") from error
    try:
        _verify_directory_identity(os.fstat(descriptor), expected, path)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_directory_identity(
    opened: os.stat_result,
    expected: os.stat_result,
    path: str,
) -> None:
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != expected.st_dev
        or opened.st_ino != expected.st_ino
    ):
        raise SourceArtifactError(f"source directory changed while archiving: {path}")


def materialize_source_artifact(
    artifact: SourceArtifact | bytes,
    destination: Path,
    *,
    limits: WorkspaceLimits = DEFAULT_WORKSPACE_LIMITS,
) -> WorkspaceManifest:
    """Validate and materialize ``artifact`` into an existing empty directory.

    Validation, including every file's digest, completes before a byte is
    written to ``destination``.  The resulting tree contains only ordinary
    files/directories, all without write bits, and the returned manifest
    captures its canonical content identity.
    """

    payload = _artifact_payload(artifact, limits)
    target = Path(destination)
    _require_empty_real_directory(target)
    validated = _validate_archive(payload, limits)
    if (
        isinstance(artifact, SourceArtifact)
        and artifact.manifest_digest is not None
        and artifact.manifest_digest != validated.manifest.digest
    ):
        raise SourceArtifactError("source artifact manifest digest does not match contents")
    try:
        _write_validated_archive(payload, target, validated, limits)
        _make_tree_read_only(target, validated.manifest.entries)
    except (OSError, tarfile.TarError) as error:
        raise SourceArtifactError("source artifact could not be materialized") from error
    return validated.manifest


def _artifact_payload(artifact: SourceArtifact | bytes, limits: WorkspaceLimits) -> bytes:
    payload = artifact.archive if isinstance(artifact, SourceArtifact) else artifact
    if not isinstance(payload, bytes):
        raise TypeError("source artifact must be SourceArtifact or bytes")
    if len(payload) > limits.max_archive_bytes:
        raise SourceArtifactError("source artifact exceeds max_archive_bytes")
    return payload


def _require_empty_real_directory(destination: Path) -> None:
    try:
        metadata = destination.lstat()
    except OSError as error:
        raise SourceArtifactError("workspace destination is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SourceArtifactError("workspace destination must be a real directory")
    try:
        if any(destination.iterdir()):
            raise SourceArtifactError("workspace destination must be empty")
    except OSError as error:
        raise SourceArtifactError("workspace destination could not be inspected") from error


def _validate_archive(payload: bytes, limits: WorkspaceLimits) -> _ValidatedArchive:
    files: list[_ValidatedFile] = []
    declared_directories: set[str] = set()
    declared_paths: set[str] = set()
    file_paths: set[str] = set()
    entries = 0
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive:
                path = _canonical_member_path(member.name, limits)
                entries += 1
                if entries > limits.max_entries:
                    raise SourceArtifactError("source artifact exceeds max_entries")
                _reject_unsafe_member(member, path)
                _reject_collision(path, member.isdir(), declared_paths, file_paths)
                declared_paths.add(path)
                if member.isdir():
                    declared_directories.add(path)
                    continue
                if member.size > limits.max_file_bytes:
                    raise SourceArtifactError(f"source file exceeds max_file_bytes: {path}")
                total_bytes += member.size
                if total_bytes > limits.max_workspace_bytes:
                    raise SourceArtifactError("source artifact exceeds max_workspace_bytes")
                digest = _hash_member(archive, member, path)
                files.append(
                    _ValidatedFile(
                        path=path,
                        size=member.size,
                        sha256=digest,
                        executable=bool(member.mode & 0o111),
                    )
                )
                file_paths.add(path)
    except SourceArtifactError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise SourceArtifactError("source artifact is not a readable tar archive") from error

    directories = _all_directories(declared_directories, (file.path for file in files))
    manifest_entries = [
        WorkspaceEntry(path=path, kind="directory", mode=0o555)
        for path in sorted(directories)
    ]
    manifest_entries.extend(
        WorkspaceEntry(
            path=file.path,
            kind="file",
            mode=0o555 if file.executable else 0o444,
            size=file.size,
            sha256=file.sha256,
        )
        for file in sorted(files, key=lambda item: item.path)
    )
    manifest = WorkspaceManifest(
        entries=tuple(sorted(manifest_entries, key=lambda entry: entry.path)),
        file_count=len(files),
        total_bytes=total_bytes,
    )
    return _ValidatedArchive(
        files=tuple(sorted(files, key=lambda item: item.path)),
        directories=frozenset(directories),
        manifest=manifest,
    )


def _canonical_member_path(name: str, limits: WorkspaceLimits) -> str:
    if not isinstance(name, str) or not name:
        raise SourceArtifactError("source artifact contains an empty path")
    if "\x00" in name or "\\" in name or name.startswith("/"):
        raise SourceArtifactError(f"source artifact contains an unsafe path: {name!r}")
    # A single trailing slash is conventional for directory members.  It is
    # normalized, while repeated/internal slashes are aliases and therefore
    # rejected before collision checks.
    core = name[:-1] if name.endswith("/") else name
    if not core or core.endswith("/") or "//" in core:
        raise SourceArtifactError(f"source artifact contains a non-canonical path: {name!r}")
    try:
        encoded = core.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceArtifactError("source artifact path is not valid UTF-8") from error
    if len(encoded) > limits.max_path_bytes:
        raise SourceArtifactError("source artifact path exceeds max_path_bytes")
    parts = core.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceArtifactError(f"source artifact contains a traversal path: {name!r}")
    # ``PurePosixPath`` catches absolute path forms not represented by a
    # leading slash (and documents that this is a POSIX archive namespace).
    if PurePosixPath(core).is_absolute():  # pragma: no cover - guarded above
        raise SourceArtifactError(f"source artifact contains an absolute path: {name!r}")
    return core


def _reject_unsafe_member(member: tarfile.TarInfo, path: str) -> None:
    # ``isreg`` can include GNU sparse members on some Python versions.  They
    # can claim a huge logical size with tiny input, so reject them explicitly.
    if getattr(member, "issparse", lambda: False)() or not (member.isdir() or member.isreg()):
        raise SourceArtifactError(f"source artifact contains unsupported member type: {path}")
    if not member.isdir() and member.name.endswith("/"):
        raise SourceArtifactError(f"source artifact contains a non-canonical path: {member.name!r}")
    mode = stat.S_IMODE(member.mode)
    unsafe_bits = (
        stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
    )
    if mode & unsafe_bits:
        raise SourceArtifactError(f"source artifact contains an unsafe mode: {path}")


def _reject_collision(
    path: str,
    is_directory: bool,
    declared_paths: set[str],
    file_paths: set[str],
) -> None:
    if path in declared_paths:
        raise SourceArtifactError(f"source artifact contains a duplicate path: {path}")
    parents = path.split("/")
    for index in range(1, len(parents)):
        if "/".join(parents[:index]) in file_paths:
            raise SourceArtifactError(f"source artifact path collides with a file: {path}")
    if not is_directory and any(existing.startswith(f"{path}/") for existing in declared_paths):
        raise SourceArtifactError(f"source artifact file collides with a child path: {path}")


def _hash_member(archive: tarfile.TarFile, member: tarfile.TarInfo, path: str) -> str:
    source = archive.extractfile(member)
    if source is None:  # pragma: no cover - regular tar members always return a reader
        raise SourceArtifactError(f"source artifact file cannot be read: {path}")
    remaining = member.size
    digest = hashlib.sha256()
    try:
        with source:
            while remaining:
                chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise SourceArtifactError(f"source artifact file is truncated: {path}")
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise SourceArtifactError(f"source artifact file exceeds its declared size: {path}")
    except (OSError, EOFError) as error:
        raise SourceArtifactError(f"source artifact file cannot be read: {path}") from error
    return digest.hexdigest()


def _all_directories(declared: set[str], file_paths: Iterable[str]) -> set[str]:
    directories = set(declared)
    for path in [*declared, *file_paths]:
        parts = path.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return directories


def _write_validated_archive(
    payload: bytes,
    destination: Path,
    validated: _ValidatedArchive,
    limits: WorkspaceLimits,
) -> None:
    # Materialize directories before files, using canonical names from the first
    # validation pass rather than archive member names.  This keeps the second
    # pass a content transfer, not a second interpretation of archive paths.
    for path in sorted(validated.directories, key=lambda item: (item.count("/"), item)):
        (destination / path).mkdir(mode=0o700)
    expected_by_path = {file.path: file for file in validated.files}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive:
            if member.isdir():
                continue
            path = _canonical_member_path(member.name, limits)
            expected = expected_by_path.get(path)
            if expected is None:
                raise SourceArtifactError("source artifact changed during materialization")
            _copy_member(archive, member, destination / path, expected)


def _copy_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
    expected: _ValidatedFile,
) -> None:
    source = archive.extractfile(member)
    if source is None:  # pragma: no cover - regular tar members always return a reader
        raise SourceArtifactError(f"source artifact file cannot be read: {expected.path}")
    digest = hashlib.sha256()
    remaining = expected.size
    try:
        with source, destination.open("xb") as output:
            while remaining:
                chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise SourceArtifactError(f"source artifact file is truncated: {expected.path}")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise SourceArtifactError(
                    f"source artifact file exceeds its declared size: {expected.path}"
                )
    except (OSError, EOFError) as error:
        raise SourceArtifactError(
            f"source artifact file could not be written: {expected.path}"
        ) from error
    if digest.hexdigest() != expected.sha256:
        raise SourceArtifactError(f"source artifact file digest changed: {expected.path}")
    destination.chmod(0o555 if expected.executable else 0o444)


def _make_tree_read_only(destination: Path, entries: tuple[WorkspaceEntry, ...]) -> None:
    # Directories are set last so construction can create their children.  The
    # root is part of the workspace boundary too, not merely its contents.
    directories = [entry.path for entry in entries if entry.kind == "directory"]
    for path in sorted(directories, key=lambda item: item.count("/"), reverse=True):
        (destination / path).chmod(0o555)
    destination.chmod(0o555)
