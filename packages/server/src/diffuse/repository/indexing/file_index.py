"""Build the immutable, literal-searchable file corpus for one repository."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .chunker import iter_repository_files


@dataclass(frozen=True)
class IndexedFile:
    """One safe, Git-tracked text file preserved verbatim for literal search."""

    file_path: str
    content: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def index_repository_files(repo_root: Path) -> list[IndexedFile]:
    """Read the same safe source-file set as chunking, without chunk boundaries.

    This deliberately inherits the tracked-file, binary, sensitive-path, and
    size protections from ``iter_repository_files``.  The code-search corpus
    therefore cannot expose a file the normal immutable index would omit.
    """

    files: list[IndexedFile] = []
    for path in iter_repository_files(repo_root):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        files.append(
            IndexedFile(
                file_path=path.relative_to(repo_root).as_posix(),
                content=raw.decode("utf-8", errors="ignore"),
            )
        )
    return files
