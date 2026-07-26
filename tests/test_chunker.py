from pathlib import Path

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
