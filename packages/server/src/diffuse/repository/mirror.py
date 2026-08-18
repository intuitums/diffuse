"""Credential-safe bare repository mirrors and exact-commit worktrees."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from diffuse.github.app import github_token
from diffuse.repository.registry import RegisteredRepository

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
DEFAULT_REPOSITORY_ROOT = "/var/lib/diffuse/repositories"
DEFAULT_MAX_REPOSITORY_BYTES = 5 * 1024**3
# A nested-tree "git bomb" expands to more paths than any reviewable repository
# holds, so the size scan itself has to stop long before it walks all of them.
MAX_CHECKOUT_TREE_ENTRIES = 2_000_000
STREAM_CHUNK_BYTES = 65536


class RepositoryMirrorError(RuntimeError):
    pass


def max_repository_bytes() -> int:
    """Bound the disk one untrusted repository may take from a shared host."""
    value = int(
        os.environ.get(
            "DIFFUSE_MAX_REPOSITORY_BYTES",
            str(DEFAULT_MAX_REPOSITORY_BYTES),
        )
    )
    if value <= 0:
        raise ValueError("DIFFUSE_MAX_REPOSITORY_BYTES must be positive")
    return value


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()


def _tree_entry_bytes(record: bytes) -> int:
    # `ls-tree --long` records are "<mode> <type> <object> <size>\t<path>", and
    # submodule entries report "-" instead of a size.
    header = record.split(b"\t", 1)[0].split()
    if len(header) < 4 or not header[3].isdigit():
        return 0
    return int(header[3])


def git_askpass_path() -> Path:
    configured = os.environ.get("DIFFUSE_GIT_ASKPASS")
    path = Path(configured) if configured else Path(__file__).with_name("git_askpass.sh")
    if configured and not path.is_absolute():
        raise ValueError("DIFFUSE_GIT_ASKPASS must be an absolute path")
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise RepositoryMirrorError("Git askpass helper is missing or not executable")
    return path


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
        # `x-access-token` is already the username a GitHub App installation
        # token clones with, so this line needed no change when App
        # authentication landed -- only the credential behind it did.
        token = github_token()
        username = "x-access-token"
        environment.update(
            {
                "DIFFUSE_GIT_TOKEN": token,
                "DIFFUSE_GIT_USERNAME": username,
                "GIT_ASKPASS": str(git_askpass_path()),
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
        process = subprocess.Popen(
            ["git", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._git_environment(),
            # Git delegates fetching and packing to helper processes that survive
            # killing the parent, so each invocation gets its own session that the
            # timeout can tear down as a whole.
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            if check:
                raise RepositoryMirrorError(
                    f"Git operation timed out: {operation}"
                ) from None
        if check and process.returncode:
            raise RepositoryMirrorError(f"Git operation failed: {operation}")
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )

    def _exceeds_byte_ceiling(self, path: Path) -> bool:
        limit = max_repository_bytes()
        total = 0
        for directory, _directories, file_names in os.walk(path, followlinks=False):
            for file_name in file_names:
                try:
                    total += os.lstat(os.path.join(directory, file_name)).st_size
                except OSError:
                    continue
                if total > limit:
                    # The verdict can no longer change and a hostile repository can
                    # hold millions of entries, so stop walking once it is decided.
                    return True
        return False

    def _checkout_exceeds_byte_ceiling(self, commit_sha: str) -> bool:
        limit = max_repository_bytes()
        process = subprocess.Popen(
            [
                "git",
                f"--git-dir={self.mirror_path}",
                "ls-tree",
                "-r",
                "-z",
                "--long",
                "--end-of-options",
                commit_sha,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._git_environment(),
            start_new_session=True,
        )
        stream = process.stdout
        total = 0
        entries = 0
        pending = b""
        try:
            while chunk := stream.read(STREAM_CHUNK_BYTES):
                records = (pending + chunk).split(b"\0")
                pending = records.pop()
                for record in records:
                    entries += 1
                    total += _tree_entry_bytes(record)
                    if total > limit or entries > MAX_CHECKOUT_TREE_ENTRIES:
                        return True
        finally:
            # The stream is abandoned as soon as the ceiling is crossed, so the
            # reader must not leave git writing into a pipe nobody drains.
            if process.poll() is None:
                _terminate_process_group(process)
            stream.close()
            process.wait()
        return False

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
                # A signed GitHub event can rename or transfer an already-bound
                # repository. Its immutable GitHub ID is what authorized the
                # database clone URL update, so keep the same bare mirror and
                # safely move origin to the new canonical URL. Repositories
                # without that bound identity retain the strict old guard.
                if self.repository.github_repository_id is None:
                    raise RepositoryMirrorError(
                        "Existing repository mirror has an unexpected remote"
                    )
                self._run_git(
                    [
                        f"--git-dir={self.mirror_path}",
                        "remote",
                        "set-url",
                        "origin",
                        self.repository.clone_url,
                    ],
                    operation="update_renamed_remote",
                )
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
            if self._exceeds_byte_ceiling(temporary_mirror):
                raise RepositoryMirrorError("Repository mirror exceeds the configured size limit")
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
        if self._exceeds_byte_ceiling(self.mirror_path):
            raise RepositoryMirrorError("Repository mirror exceeds the configured size limit")

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
            # The packed mirror says nothing about how far the tree expands, so the
            # working copy is measured before any of it reaches the disk.
            if self._checkout_exceeds_byte_ceiling(resolved_commit):
                raise RepositoryMirrorError("Repository checkout exceeds the configured size limit")

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
