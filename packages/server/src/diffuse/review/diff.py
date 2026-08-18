"""Parse unified Git diffs into reviewable files and exact inline locations."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _path(value: str) -> str | None:
    value = value.strip()
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        try:
            value = shlex.split(value)[0]
        except (ValueError, IndexError):
            return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value or None


@dataclass(frozen=True)
class DiffEntry:
    marker: str
    text: str
    old_line: int | None
    new_line: int | None


@dataclass
class DiffFile:
    old_path: str | None
    new_path: str | None
    raw_text: str
    entries: list[DiffEntry] = field(default_factory=list)
    left_lines: set[int] = field(default_factory=set)
    right_lines: set[int] = field(default_factory=set)

    @property
    def comment_path(self) -> str | None:
        return self.new_path or self.old_path

    def contains(self, side: str, line: int) -> bool:
        lines = self.right_lines if side == "RIGHT" else self.left_lines
        return line in lines

    def snippet(self, side: str, line: int, *, radius: int = 5) -> str:
        target_index = None
        for index, entry in enumerate(self.entries):
            candidate_line = entry.new_line if side == "RIGHT" else entry.old_line
            if candidate_line == line and entry.marker in {"+", "-"}:
                target_index = index
                break
        if target_index is None:
            return ""

        output: list[str] = []
        start = max(0, target_index - radius)
        end = min(len(self.entries), target_index + radius + 1)
        for entry in self.entries[start:end]:
            old = str(entry.old_line) if entry.old_line is not None else "-"
            new = str(entry.new_line) if entry.new_line is not None else "-"
            output.append(f"{old:>6} {new:>6} {entry.marker}{entry.text}")
        return "\n".join(output)


@dataclass(frozen=True)
class ParsedDiff:
    files: tuple[DiffFile, ...]

    def file(self, path: str) -> DiffFile | None:
        return next((file for file in self.files if file.comment_path == path), None)

    def is_commentable(self, path: str, side: str, line: int) -> bool:
        file = self.file(path)
        return bool(file and file.contains(side, line))

    def snippet(self, path: str, side: str, line: int) -> str:
        file = self.file(path)
        return file.snippet(side, line) if file else ""


def _parse_file(raw_text: str) -> DiffFile:
    old_path: str | None = None
    new_path: str | None = None
    entries: list[DiffEntry] = []
    left_lines: set[int] = set()
    right_lines: set[int] = set()
    old_line: int | None = None
    new_line: int | None = None

    for raw_line in raw_text.splitlines():
        if raw_line.startswith("--- "):
            old_path = _path(raw_line[4:])
            continue
        if raw_line.startswith("+++ "):
            new_path = _path(raw_line[4:])
            continue
        match = HUNK_PATTERN.match(raw_line)
        if match:
            old_line = int(match.group("old_start"))
            new_line = int(match.group("new_start"))
            continue
        if old_line is None or new_line is None or not raw_line:
            continue

        marker = raw_line[0]
        text = raw_line[1:]
        if marker == "+":
            entries.append(DiffEntry(marker, text, None, new_line))
            right_lines.add(new_line)
            new_line += 1
        elif marker == "-":
            entries.append(DiffEntry(marker, text, old_line, None))
            left_lines.add(old_line)
            old_line += 1
        elif marker == " ":
            entries.append(DiffEntry(marker, text, old_line, new_line))
            old_line += 1
            new_line += 1

    return DiffFile(
        old_path=old_path,
        new_path=new_path,
        raw_text=raw_text,
        entries=entries,
        left_lines=left_lines,
        right_lines=right_lines,
    )


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    sections: list[str] = []
    current: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                sections.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append("\n".join(current))

    files = tuple(file for section in sections if (file := _parse_file(section)).comment_path)
    return ParsedDiff(files=files)


def pack_diff_files(
    parsed: ParsedDiff,
    *,
    max_chars: int,
    max_chunks: int,
) -> tuple[list[str], set[str]]:
    if max_chars <= 0 or max_chunks <= 0:
        raise ValueError("Diff chunk limits must be positive")

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    reviewed_paths: set[str] = set()

    for file in parsed.files:
        if len(chunks) >= max_chunks:
            break
        section = file.raw_text
        if len(section) > max_chars:
            marker = "\n... file diff truncated by Diffuse review budget ...\n"
            if max_chars <= len(marker):
                section = section[:max_chars]
            else:
                remaining = max_chars - len(marker)
                head = (remaining + 1) // 2
                tail = remaining // 2
                section = section[:head] + marker + section[-tail:]

        added_length = len(section) + (2 if current else 0)
        if current and current_length + added_length > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_length = 0
            if len(chunks) >= max_chunks:
                break
        current.append(section)
        current_length += added_length
        if file.comment_path:
            reviewed_paths.add(file.comment_path)

    if current and len(chunks) < max_chunks:
        chunks.append("\n\n".join(current))
    return chunks, reviewed_paths
