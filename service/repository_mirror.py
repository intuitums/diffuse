"""Credential-safe bare repository mirrors and exact-commit worktrees."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from service.repositories import RegisteredRepository

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
DEFAULT_REPOSITORY_ROOT = "/var/lib/diffuse/repositories"


class RepositoryMirrorError(RuntimeError):
    pass


class RepositoryMirror:
    def __init__(
        self,
        repository: RegisteredRepository,
        *,
        root: str | Path | None = None,
    ) -> None:
        self.repository = repository
        configured_root = root or os.environ.get(
            "DIFFUSE_REPOSITORY_ROOT",
            DEFAULT_REPOSITORY_ROOT,
        )
        self.root = Path(configured_root)
        self.mirror_path = self.root / f"{repository.id}.git"
        self.lock_path = self.root / f".{repository.id}.lock"

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise RepositoryMirrorError("Repository root must be a real directory")
        if self.mirror_path.is_symlink():
            raise RepositoryMirrorError("Repository mirror cannot be a symbolic link")

    def _git_environment(self) -> dict[str, str]:
        allowed_environment = {
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "GIT_SSL_CAINFO",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in allowed_environment
        }
        token_variable = (
            "GITHUB_TOKEN" if self.repository.scm_provider == "github" else "GITLAB_TOKEN"
        )
        username = "x-access-token" if self.repository.scm_provider == "github" else "oauth2"
        token = os.environ.get(token_variable, "")
        environment.update(
            {
                "DIFFUSE_GIT_TOKEN": token,
                "DIFFUSE_GIT_USERNAME": username,
                "GIT_ASKPASS": str(Path(__file__).with_name("git_askpass.sh")),
                "GIT_ASKPASS_REQUIRE": "force",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "http.followRedirects",
                "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "core.hooksPath",
                "GIT_CONFIG_VALUE_2": "/dev/null",
            }
        )
        return environment

    def _run_git(
        self,
        arguments: list[str],
        *,
        operation: str,
        timeout: int = 600,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=self._git_environment(),
        )
        if check and result.returncode:
            raise RepositoryMirrorError(f"Git operation failed: {operation}")
        return result

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._ensure_root()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as error:
            raise RepositoryMirrorError("Unable to open repository lock safely") from error
        with os.fdopen(descriptor, "a+b") as lock_file:
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield

    def _ensure_mirror_locked(self) -> None:
        if self.mirror_path.exists():
            bare = self._run_git(
                [
                    f"--git-dir={self.mirror_path}",
                    "rev-parse",
                    "--is-bare-repository",
                ],
                operation="validate_mirror",
            )
            if bare.stdout.strip() != "true":
                raise RepositoryMirrorError("Existing repository mirror is not bare")
            remote = self._run_git(
                [
                    f"--git-dir={self.mirror_path}",
                    "remote",
                    "get-url",
                    "origin",
                ],
                operation="validate_remote",
            )
            if remote.stdout.strip().rstrip("/") != self.repository.clone_url.rstrip("/"):
                raise RepositoryMirrorError("Existing repository mirror has an unexpected remote")
            return

        temporary_parent = Path(
            tempfile.mkdtemp(
                prefix=f".{self.repository.id}-clone-",
                dir=self.root,
            )
        )
        temporary_mirror = temporary_parent / "mirror.git"
        try:
            self._run_git(
                [
                    "clone",
                    "--mirror",
                    "--",
                    self.repository.clone_url,
                    str(temporary_mirror),
                ],
                operation="clone_mirror",
            )
            os.replace(temporary_mirror, self.mirror_path)
        finally:
            shutil.rmtree(temporary_parent, ignore_errors=True)

    def _fetch_locked(self) -> None:
        self._ensure_mirror_locked()
        self._run_git(
            [
                f"--git-dir={self.mirror_path}",
                "fetch",
                "--prune",
                "--tags",
                "origin",
                "+refs/heads/*:refs/heads/*",
            ],
            operation="fetch_mirror",
        )

    def _resolve_locked(self, revision: str) -> str:
        result = self._run_git(
            [
                f"--git-dir={self.mirror_path}",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            operation="resolve_revision",
        )
        commit_sha = result.stdout.strip().lower()
        if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise RepositoryMirrorError("Git returned an invalid commit identifier")
        return commit_sha

    def resolve_default_commit(self) -> str:
        with self._lock():
            self._fetch_locked()
            return self._resolve_locked(f"refs/heads/{self.repository.default_branch}")

    @contextmanager
    def checkout(self, commit_sha: str) -> Iterator[Path]:
        if not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise ValueError("commit_sha must be a full Git commit identifier")

        with self._lock():
            self._fetch_locked()
            resolved_commit = self._resolve_locked(commit_sha)
            if resolved_commit != commit_sha.lower():
                raise RepositoryMirrorError("Fetched revision does not match the requested commit")

            temporary_parent = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.repository.id}-worktree-",
                    dir=self.root,
                )
            )
            worktree = temporary_parent / "checkout"
            added = False
            try:
                self._run_git(
                    [
                        f"--git-dir={self.mirror_path}",
                        "worktree",
                        "add",
                        "--detach",
                        str(worktree),
                        resolved_commit,
                    ],
                    operation="create_worktree",
                )
                added = True
                yield worktree
            finally:
                if added:
                    self._run_git(
                        [
                            f"--git-dir={self.mirror_path}",
                            "worktree",
                            "remove",
                            "--force",
                            str(worktree),
                        ],
                        operation="remove_worktree",
                        check=False,
                    )
                shutil.rmtree(temporary_parent, ignore_errors=True)
