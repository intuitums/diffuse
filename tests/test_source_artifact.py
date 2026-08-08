"""Security and determinism coverage for isolated source artifacts."""

from __future__ import annotations

import io
import os
import stat
import tarfile

import pytest

from service.review import workspace
from service.review.workspace import (
    SourceArtifact,
    SourceArtifactError,
    WorkspaceLimits,
    build_source_artifact,
    materialize_source_artifact,
)


def _tar(*members: tuple[str, bytes | None, int, bytes]) -> bytes:
    """Build a small archive as ``(name, data-or-None, mode, type)`` entries."""

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, mode, member_type in members:
            member = tarfile.TarInfo(name)
            member.type = member_type
            member.mode = mode
            if content is not None:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            else:
                archive.addfile(member)
    return output.getvalue()


def test_materializes_a_valid_archive_read_only_with_stable_manifest(tmp_path):
    artifact = SourceArtifact(
        _tar(
            ("bin/", None, 0o555, tarfile.DIRTYPE),
            ("bin/review", b"#!/bin/sh\n", 0o555, tarfile.REGTYPE),
            ("README.md", b"hello\n", 0o444, tarfile.REGTYPE),
        )
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_manifest = materialize_source_artifact(artifact, first)
    second_manifest = materialize_source_artifact(artifact.archive_bytes, second)

    assert (first / "bin/review").read_bytes() == b"#!/bin/sh\n"
    assert first_manifest == second_manifest
    assert first_manifest.digest == second_manifest.digest
    assert first_manifest.file_count == 2
    assert first_manifest.total_bytes == len(b"#!/bin/sh\n") + len(b"hello\n")
    assert stat.S_IMODE((first / "README.md").stat().st_mode) == 0o444
    assert stat.S_IMODE((first / "bin/review").stat().st_mode) == 0o555
    assert stat.S_IMODE((first / "bin").stat().st_mode) == 0o555
    assert stat.S_IMODE(first.stat().st_mode) == 0o555


@pytest.mark.parametrize(
    "members,match",
    [
        ((("../escape", b"x", 0o444, tarfile.REGTYPE),), "traversal"),
        ((("/escape", b"x", 0o444, tarfile.REGTYPE),), "unsafe path"),
        ((("dir\\escape", b"x", 0o444, tarfile.REGTYPE),), "unsafe path"),
        (
            (
                ("link", None, 0o444, tarfile.SYMTYPE),
            ),
            "unsupported member type",
        ),
        (
            (
                ("hard-link", None, 0o444, tarfile.LNKTYPE),
            ),
            "unsupported member type",
        ),
        (
            (
                ("device", None, 0o444, tarfile.CHRTYPE),
            ),
            "unsupported member type",
        ),
        (
            (
                ("same", b"a", 0o444, tarfile.REGTYPE),
                ("same", b"b", 0o444, tarfile.REGTYPE),
            ),
            "duplicate",
        ),
        (
            (
                ("file", b"a", 0o444, tarfile.REGTYPE),
                ("file/child", b"b", 0o444, tarfile.REGTYPE),
            ),
            "collides",
        ),
        ((("writable", b"x", 0o644, tarfile.REGTYPE),), "unsafe mode"),
        ((("suid", b"x", 0o444 | stat.S_ISUID, tarfile.REGTYPE),), "unsafe mode"),
    ],
)
def test_rejects_unsafe_archive_members(tmp_path, members, match):
    destination = tmp_path / "workspace"
    destination.mkdir()

    with pytest.raises(SourceArtifactError, match=match):
        materialize_source_artifact(_tar(*members), destination)

    assert list(destination.iterdir()) == []


def test_rejects_nonempty_or_linked_destination(tmp_path):
    artifact = _tar(("safe", b"x", 0o444, tarfile.REGTYPE))
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "already-there").write_text("x")
    with pytest.raises(SourceArtifactError, match="empty"):
        materialize_source_artifact(artifact, nonempty)

    real_destination = tmp_path / "real"
    real_destination.mkdir()
    linked_destination = tmp_path / "linked"
    linked_destination.symlink_to(real_destination, target_is_directory=True)
    with pytest.raises(SourceArtifactError, match="real directory"):
        materialize_source_artifact(artifact, linked_destination)


def test_refuses_an_artifact_whose_bound_manifest_does_not_match(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.py").write_text("answer = 42\n")
    built = build_source_artifact(source)
    artifact = SourceArtifact(built.archive, manifest_digest="0" * 64)
    destination = tmp_path / "workspace"
    destination.mkdir()

    with pytest.raises(SourceArtifactError, match="manifest digest"):
        materialize_source_artifact(artifact, destination)

    assert list(destination.iterdir()) == []


def test_enforces_archive_member_file_and_total_budgets(tmp_path):
    artifact = _tar(("large", b"abcd", 0o444, tarfile.REGTYPE))

    destination = tmp_path / "archive"
    destination.mkdir()
    with pytest.raises(SourceArtifactError, match="max_archive_bytes"):
        materialize_source_artifact(
            artifact,
            destination,
            limits=WorkspaceLimits(
                max_archive_bytes=1,
                max_workspace_bytes=100,
                max_entries=10,
                max_file_bytes=100,
                max_path_bytes=100,
            ),
        )

    destination = tmp_path / "file"
    destination.mkdir()
    with pytest.raises(SourceArtifactError, match="max_file_bytes"):
        materialize_source_artifact(
            artifact,
            destination,
            limits=WorkspaceLimits(
                max_archive_bytes=100_000,
                max_workspace_bytes=100,
                max_entries=10,
                max_file_bytes=3,
                max_path_bytes=100,
            ),
        )

    total_artifact = _tar(
        ("first", b"abc", 0o444, tarfile.REGTYPE),
        ("second", b"def", 0o444, tarfile.REGTYPE),
    )
    destination = tmp_path / "total"
    destination.mkdir()
    with pytest.raises(SourceArtifactError, match="max_workspace_bytes"):
        materialize_source_artifact(
            total_artifact,
            destination,
            limits=WorkspaceLimits(
                max_archive_bytes=100_000,
                max_workspace_bytes=3,
                max_entries=10,
                max_file_bytes=3,
                max_path_bytes=100,
            ),
        )


def test_build_source_artifact_is_deterministic_and_rejects_links_and_special_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "bin").mkdir()
    executable = source / "bin" / "review"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    (source / "README.md").write_text("hello\n")
    (source / ".git").write_text("gitdir: /private/control-plane/path\n")

    first = build_source_artifact(source)
    second = build_source_artifact(source)

    assert first.digest == second.digest
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = materialize_source_artifact(first, workspace)
    assert manifest.file_count == 2
    assert (workspace / "README.md").read_text() == "hello\n"
    assert not (workspace / ".git").exists()

    (source / "outside").symlink_to(tmp_path / "outside")
    with pytest.raises(SourceArtifactError, match="symlink"):
        build_source_artifact(source)

    (source / "outside").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(source / "pipe")
        with pytest.raises(SourceArtifactError, match="regular file or directory"):
            build_source_artifact(source)


def test_build_refuses_a_directory_replaced_by_a_symlink_during_traversal(tmp_path, monkeypatch):
    """A checkout race must not redirect a recursive scan outside its root."""

    source = tmp_path / "source"
    source.mkdir()
    checked_directory = source / "checked"
    checked_directory.mkdir()
    (checked_directory / "reviewed.py").write_text("reviewed = True\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("must_not_be_archived = True\n")

    original = workspace._add_directory_member

    def swap_checked_directory(archive, path):
        original(archive, path)
        if path == "checked":
            checked_directory.rename(source / "checked-original")
            (source / "checked").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(workspace, "_add_directory_member", swap_checked_directory)

    with pytest.raises(SourceArtifactError, match="could not be opened"):
        build_source_artifact(source)
