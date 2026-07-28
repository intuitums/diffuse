import json
import subprocess
import sys
from pathlib import Path

import pytest

from indexer import chunker
from indexer.chunker import _iter_files, chunk_file


def test_python_chunks_keep_preamble_and_extract_async_symbol(tmp_path: Path):
    source = tmp_path / "example.py"
    source.write_text(
        '"""Module docs."""\n\nimport os\n\nasync def fetch_item():\n    return os.getcwd()\n'
    )

    chunks = chunk_file(source, tmp_path)

    assert [chunk.start_line for chunk in chunks] == [1, 5]
    assert chunks[0].symbol_name is None
    assert chunks[1].symbol_name == "fetch_item"
    assert "import os" in chunks[0].content


def test_repository_walk_skips_untracked_env_and_private_keys(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=value")
    (tmp_path / "private.pem").write_text("not really a key")
    (tmp_path / ".env.example").write_text("SECRET=")
    (tmp_path / "main.py").write_text("print('ok')")

    names = {path.name for path in _iter_files(tmp_path)}

    assert ".env" not in names
    assert "private.pem" not in names
    assert {".env.example", "main.py"} <= names


def test_binary_file_is_not_chunked(tmp_path: Path):
    binary = tmp_path / "fixture.bin"
    binary.write_bytes(b"prefix\0suffix")

    assert chunk_file(binary, tmp_path) == []


def test_crlf_line_endings_preserve_definition_line_numbers(tmp_path: Path):
    source = tmp_path / "windows.py"
    source.write_bytes(
        b"import os\r\n\r\ndef first():\r\n    pass\r\n\r\ndef second():\r\n    pass\r\n"
    )

    chunks = chunk_file(source, tmp_path)

    assert [(chunk.start_line, chunk.symbol_name) for chunk in chunks] == [
        (1, None),
        (3, "first"),
        (6, "second"),
    ]


def test_a_file_that_exhausts_the_stack_is_skipped_not_fatal(monkeypatch, tmp_path: Path):
    (tmp_path / "hostile.py").write_text("value = " + " + ".join(["1"] * 20000) + "\n")
    (tmp_path / "healthy.py").write_text("def normalize(value):\n    return value\n")
    original_chunk_file = chunker.chunk_file

    def failing_chunk_file(path: Path, repo_root: Path, **keywords):
        if path.name == "hostile.py":
            raise RecursionError("maximum recursion depth exceeded")
        return original_chunk_file(path, repo_root, **keywords)

    monkeypatch.setattr(chunker, "chunk_file", failing_chunk_file)

    chunks = chunker.chunk_repo(tmp_path)

    assert {chunk.file_path for chunk in chunks} == {"healthy.py"}


# Run the pathological files in a child interpreter: a backtracking matcher holds the GIL,
# so an in-process guard would hang the whole suite instead of reporting a failure.
_JAVA_COMPLEXITY_PROBE = r"""
import json
import tempfile
import time
from pathlib import Path

from indexer.chunker import MAX_FILE_BYTES, chunk_file

root = Path(tempfile.mkdtemp())
measured = {}

# The comment keeps the file non-empty while leaving tree-sitter with no chunkable symbol,
# which is what routes the blanks into the definition patterns.
blanks = root / "blanks.java"
blanks.write_text("// nothing to declare\n" + " " * MAX_FILE_BYTES + "\n")
started = time.perf_counter()
measured["blank_chunks"] = len(chunk_file(blanks, root))
measured["blank_seconds"] = time.perf_counter() - started

# Matches the definition pattern, so the symbol-name fallback runs over the whole line.
wide = root / "wide.java"
wide.write_text("int " + "a" * MAX_FILE_BYTES + " \n")
started = time.perf_counter()
measured["wide_chunks"] = len(chunk_file(wide, root))
measured["wide_seconds"] = time.perf_counter() - started

print(json.dumps(measured))
"""


def test_a_java_file_of_blanks_cannot_wedge_the_indexer():
    """Pushed `.java` files are attacker-supplied, and a file of blanks took cubic time.

    The `.java` definition pattern spelled its type with a class that contained a space
    and then required `\\s+` after it, so a whitespace run no word terminated forced the
    engine through every split of that run: 1.6KB of blanks already cost five seconds and
    `MAX_FILE_BYTES` allows 250 times that. One such file outlived the worker's lease, and
    the next worker to claim the job stalled on it identically until the pool was gone.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _JAVA_COMPLEXITY_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    measured = json.loads(completed.stdout)
    assert measured["blank_seconds"] < 1
    assert measured["wide_seconds"] < 1
    # Both files must still be indexed. A size bound that skipped them would buy the same
    # wall-clock number by dropping legitimate generated sources on the floor.
    assert measured["blank_chunks"] > 0
    assert measured["wide_chunks"] > 0


_DEFINITION_PATTERN_PROBE = r"""
import json
import time

from indexer.chunker import DEFINITION_PATTERNS, MAX_FILE_BYTES

# Runs that a pattern with two adjacent variable-length parts has to enumerate splits of:
# whitespace no word ever terminates, plus the tokens the alternatives themselves spell.
units = [
    " ", "\t", " \t", "a", "a ", "Foo(", "Map<String, ", "public ", "class ",
    "export ", "async ", "func (", "def ", "const ",
]
measured = {}
for extension, pattern in DEFINITION_PATTERNS.items():
    worst = 0.0
    for unit in units:
        text = "x\n" + unit * (MAX_FILE_BYTES // len(unit))
        started = time.perf_counter()
        for _ in pattern.finditer(text):
            pass
        worst = max(worst, time.perf_counter() - started)
    measured[extension] = worst

print(json.dumps(measured))
"""


def test_every_definition_pattern_stays_linear_on_hostile_input():
    """The whole table is the attack surface, not just the entry that was exploited.

    Every pattern here is run against repository content nobody vetted, and there is no
    timeout around `chunk_file` to catch the next one that pairs two variable-length parts
    able to match the same character. Failing here is the cheap version of that discovery.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _DEFINITION_PATTERN_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    measured = json.loads(completed.stdout)
    assert set(measured) == set(chunker.DEFINITION_PATTERNS)
    for extension, seconds in measured.items():
        assert seconds < 1, f"{extension} spent {seconds:.3f}s on {chunker.MAX_FILE_BYTES} bytes"


@pytest.mark.parametrize(
    "declaration",
    [
        "public final class OrderService extends BaseService implements Runnable {",
        "abstract static class Inner<T> {",
        "public interface Listener {",
        "public enum Status { OPEN, CLOSED }",
        "public record Point(int x, int y) {}",
        "    public void run() {",
        "    private synchronized <T extends Comparable<T>> T max(List<T> items) {",
        "    static Map<String, Object> build(String key, int value) throws IOException {",
        "    private static final Map<String, List<Integer>> CACHE = new HashMap<>();",
        "    List<? extends Number> values;",
        "    protected int counter = 0;",
        "    OrderService(String name) {",
    ],
)
def test_java_definitions_survive_the_unambiguous_pattern(declaration: str):
    """Removing the ambiguity must not remove declarations along with it.

    Modifiers, generic and wildcard return types, the four type keywords, the line an
    annotation sits above, and constructors — which have no return type for the pattern's
    first branch to match — all have to keep starting a chunk.
    """
    assert chunker.DEFINITION_PATTERNS[".java"].search(declaration) is not None


@pytest.mark.parametrize(
    "line",
    [
        "",
        "                    ",
        "\t\t\t",
        "     * Doc comment.",
        "    @Override",
        "        }",
    ],
)
def test_java_lines_that_declare_nothing_are_not_definitions(line: str):
    """Indentation was being read as a type, which is both the bug and the attack input.

    A blank line matched because its leading spaces could be split between the type and
    the separator after it, so a file of whitespace looked like an endless run of
    declarations to try. Nothing here declares anything and nothing here should match.
    """
    assert chunker.DEFINITION_PATTERNS[".java"].search(line) is None
