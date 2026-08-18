import subprocess
from pathlib import Path

import pytest
from diffuse.repository.indexing.index_repo import repository_commit


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


def test_repository_commit_requires_a_clean_tracked_worktree(tmp_path: Path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "diffuse-tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Diffuse Tests")
    source = tmp_path / "app.py"
    source.write_text("print('first')\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "initial")

    commit_sha = repository_commit(tmp_path)

    assert len(commit_sha) == 40
    source.write_text("print('changed')\n")
    with pytest.raises(ValueError, match="uncommitted changes"):
        repository_commit(tmp_path)


def test_repository_commit_rejects_non_git_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="valid HEAD"):
        repository_commit(tmp_path)
