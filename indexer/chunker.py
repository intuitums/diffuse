"""Turn Git-tracked source files into bounded, review-sized chunks."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph_models import CodeSymbol

DEFINITION_PATTERNS = {
    ".py": re.compile(r"^(?:async\s+def|def|class)\s+\w+", re.MULTILINE),
    ".js": re.compile(
        r"^(?:export\s+(?:default\s+)?)?(?:(?:async\s+)?function|class|const|let)\s+\w+",
        re.MULTILINE,
    ),
    ".jsx": re.compile(
        r"^(?:export\s+(?:default\s+)?)?(?:(?:async\s+)?function|class|const|let)\s+\w+",
        re.MULTILINE,
    ),
    ".ts": re.compile(
        r"^(?:export\s+(?:default\s+)?)?"
        r"(?:(?:async\s+)?function|class|interface|type|enum|const|let)\s+\w+",
        re.MULTILINE,
    ),
    ".tsx": re.compile(
        r"^(?:export\s+(?:default\s+)?)?"
        r"(?:(?:async\s+)?function|class|interface|type|enum|const|let)\s+\w+",
        re.MULTILINE,
    ),
    ".go": re.compile(r"^(?:func(?:\s+\([^)]*\))?|type)\s+\w+", re.MULTILINE),
    ".java": re.compile(
        r"^[ \t]*(?:(?:public|private|protected|static|final|abstract|synchronized)[ \t]+)*"
        r"(?:class|interface|enum|record|[\w<>\[\], ?]+\s+)\w+(?:\s*\(|\s+)",
        re.MULTILINE,
    ),
    ".rb": re.compile(r"^(?:def|class|module)\s+[\w:!?=]+", re.MULTILINE),
}

FALLBACK_WINDOW_LINES = 80
FALLBACK_OVERLAP_LINES = 12
MAX_DEFINITION_CHUNK_LINES = 160
DEFINITION_OVERLAP_LINES = 20
MAX_FILE_BYTES = 400_000
DEFINITION_SYMBOL_KINDS = frozenset(
    {
        "class",
        "enum",
        "function",
        "implementation",
        "interface",
        "method",
        "namespace",
        "trait",
        "type",
    }
)

SKIP_DIRS = {
    ".git",
    ".idea",
    ".next",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}
SKIP_EXTENSIONS = {
    ".avif",
    ".class",
    ".dll",
    ".dylib",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lock",
    ".map",
    ".min.js",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".svg",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}
PRIVATE_KEY_EXTENSIONS = {".key", ".p12", ".pfx", ".pem"}
SENSITIVE_FILENAMES = {
    "credentials.json",
    "service-account.json",
    "service_account.json",
}
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")


@dataclass(frozen=True)
class Chunk:
    file_path: str
    start_line: int
    end_line: int
    content: str
    symbol_name: str | None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_FILENAMES or path.suffix.lower() in PRIVATE_KEY_EXTENSIONS:
        return True
    return name.startswith(".env") and not name.endswith(ENV_TEMPLATE_SUFFIXES)


def _is_candidate(path: Path, repo_root: Path) -> bool:
    try:
        relative = path.relative_to(repo_root)
        stat = path.stat()
    except (OSError, ValueError):
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and stat.st_size <= MAX_FILE_BYTES
        and not any(part in SKIP_DIRS for part in relative.parts[:-1])
        and path.suffix.lower() not in SKIP_EXTENSIONS
        and not _is_sensitive(relative)
    )


def _git_tracked_paths(repo_root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return [
        repo_root / raw_path.decode(errors="surrogateescape")
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    ]


def _iter_files(repo_root: Path) -> Iterator[Path]:
    tracked = _git_tracked_paths(repo_root)
    candidates = tracked if tracked is not None else repo_root.rglob("*")
    for path in candidates:
        if _is_candidate(path, repo_root):
            yield path


def iter_repository_files(repo_root: Path) -> Iterator[Path]:
    """Yield indexable repository files without exposing walker internals."""
    yield from _iter_files(repo_root)


def _offset_to_line(offsets: list[int], offset: int) -> int:
    low, high = 0, len(offsets) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if offsets[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low


def _extract_symbol(line: str, extension: str) -> str | None:
    stripped = line.strip()
    patterns = {
        ".py": r"^(?:async\s+def|def|class)\s+([A-Za-z_]\w*)",
        ".js": (
            r"^(?:export\s+(?:default\s+)?)?"
            r"(?:(?:async\s+)?function|class|const|let)\s+([A-Za-z_$][\w$]*)"
        ),
        ".jsx": (
            r"^(?:export\s+(?:default\s+)?)?"
            r"(?:(?:async\s+)?function|class|const|let)\s+([A-Za-z_$][\w$]*)"
        ),
        ".ts": (
            r"^(?:export\s+(?:default\s+)?)?"
            r"(?:(?:async\s+)?function|class|interface|type|enum|const|let)\s+"
            r"([A-Za-z_$][\w$]*)"
        ),
        ".tsx": (
            r"^(?:export\s+(?:default\s+)?)?"
            r"(?:(?:async\s+)?function|class|interface|type|enum|const|let)\s+"
            r"([A-Za-z_$][\w$]*)"
        ),
        ".go": r"^(?:func(?:\s+\([^)]*\))?|type)\s+([A-Za-z_]\w*)",
        ".rb": r"^(?:def|class|module)\s+([\w:!?=]+)",
    }
    if extension in patterns:
        match = re.search(patterns[extension], stripped)
        return match.group(1) if match else None

    method_match = re.search(r"([A-Za-z_]\w*)\s*\(", stripped)
    if method_match:
        return method_match.group(1)
    type_match = re.search(r"\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)", stripped)
    return type_match.group(1) if type_match else None


def _split_span(
    start: int,
    end: int,
    symbol: str | None,
    *,
    window: int,
    overlap: int,
) -> list[tuple[int, int, str | None]]:
    spans: list[tuple[int, int, str | None]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window, end)
        spans.append((cursor, chunk_end, symbol))
        if chunk_end == end:
            break
        cursor = chunk_end - overlap
    return spans


def _chunk_by_definitions(
    text: str,
    pattern: re.Pattern[str],
    extension: str,
) -> list[tuple[int, int, str | None]]:
    lines = text.splitlines()
    matches = list(pattern.finditer(text))
    if not matches:
        return []

    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    starts = sorted({_offset_to_line(offsets, match.start()) for match in matches})
    spans: list[tuple[int, int, str | None]] = []
    if starts[0] > 0:
        spans.extend(
            _split_span(
                0,
                starts[0],
                None,
                window=MAX_DEFINITION_CHUNK_LINES,
                overlap=DEFINITION_OVERLAP_LINES,
            )
        )

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        symbol = _extract_symbol(lines[start], extension)
        spans.extend(
            _split_span(
                start,
                end,
                symbol,
                window=MAX_DEFINITION_CHUNK_LINES,
                overlap=DEFINITION_OVERLAP_LINES,
            )
        )
    return spans


def _chunk_by_window(text: str) -> list[tuple[int, int, str | None]]:
    lines = text.splitlines()
    if not lines:
        return []
    return _split_span(
        0,
        len(lines),
        None,
        window=FALLBACK_WINDOW_LINES,
        overlap=FALLBACK_OVERLAP_LINES,
    )


def _chunk_by_symbols(
    text: str,
    symbols: list[CodeSymbol],
) -> list[tuple[int, int, str | None]]:
    lines = text.splitlines()
    definitions = [
        symbol
        for symbol in symbols
        if symbol.kind in DEFINITION_SYMBOL_KINDS
        and 1 <= symbol.start_line <= len(lines)
    ]
    if not definitions:
        return []

    names_by_start: dict[int, str] = {}
    for symbol in sorted(
        definitions,
        key=lambda value: (
            value.start_line,
            value.end_line,
            value.qualified_name,
        ),
    ):
        names_by_start.setdefault(symbol.start_line - 1, symbol.name)
    starts = sorted(names_by_start)
    spans: list[tuple[int, int, str | None]] = []
    if starts[0] > 0:
        spans.extend(
            _split_span(
                0,
                starts[0],
                None,
                window=MAX_DEFINITION_CHUNK_LINES,
                overlap=DEFINITION_OVERLAP_LINES,
            )
        )
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        spans.extend(
            _split_span(
                start,
                end,
                names_by_start[start],
                window=MAX_DEFINITION_CHUNK_LINES,
                overlap=DEFINITION_OVERLAP_LINES,
            )
        )
    return spans


def chunk_file(
    path: Path,
    repo_root: Path,
    *,
    symbols: list[CodeSymbol] | None = None,
) -> list[Chunk]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw:
        return []

    text = raw.decode("utf-8", errors="ignore")
    if not text.strip():
        return []

    if symbols is None:
        # This lazy import avoids a module cycle while letting direct chunking
        # calls benefit from the same language adapters as full indexing.
        from .graph import extract_file_graph

        symbols = list(extract_file_graph(path, repo_root).symbols)

    extension = path.suffix.lower()
    spans = _chunk_by_symbols(text, symbols)
    if not spans:
        pattern = DEFINITION_PATTERNS.get(extension)
        spans = _chunk_by_definitions(text, pattern, extension) if pattern else []
    if not spans:
        spans = _chunk_by_window(text)

    lines = text.splitlines()
    relative_path = path.relative_to(repo_root).as_posix()
    chunks: list[Chunk] = []
    for start, end, symbol in spans:
        content = "\n".join(lines[start:end]).strip()
        if content:
            chunks.append(
                Chunk(
                    file_path=relative_path,
                    start_line=start + 1,
                    end_line=end,
                    content=content,
                    symbol_name=symbol,
                )
            )
    return chunks


def chunk_repo(
    repo_root: Path,
    *,
    symbols: tuple[CodeSymbol, ...] | list[CodeSymbol] | None = None,
) -> list[Chunk]:
    symbols_by_file: dict[str, list[CodeSymbol]] = {}
    if symbols is not None:
        for symbol in symbols:
            symbols_by_file.setdefault(symbol.file_path, []).append(symbol)

    chunks: list[Chunk] = []
    for path in iter_repository_files(repo_root):
        file_path = path.relative_to(repo_root).as_posix()
        chunks.extend(
            chunk_file(
                path,
                repo_root,
                symbols=symbols_by_file.get(file_path) if symbols is not None else None,
            )
        )
    return chunks
