"""Build and atomically activate an immutable repository index snapshot."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from repository_policy.discovery import discover_repository_policy
from repository_policy.store import write_repository_policy

from .chunker import chunk_repo
from .file_index import index_repository_files
from .graph import extract_repository_graph
from .store import (
    activate_snapshot,
    begin_index_snapshot,
    copy_unchanged_chunks,
    copy_unchanged_files,
    get_conn,
    get_existing_file_hashes,
    get_existing_hashes,
    mark_snapshot_failed,
    touch_snapshot,
    upsert_chunks,
    upsert_repository_files,
    validate_snapshot_ready,
    write_symbol_graph,
)

REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def repository_commit(root: Path) -> str:
    commit_result = _git(root, "rev-parse", "--verify", "HEAD")
    commit_sha = commit_result.stdout.strip()
    if commit_result.returncode or not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError(f"{root} is not a Git checkout with a valid HEAD commit")

    status_result = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status_result.returncode:
        raise RuntimeError("Unable to determine repository worktree status")
    if status_result.stdout.strip():
        raise ValueError(
            "Tracked files contain uncommitted changes; index a clean commit for reproducibility"
        )
    return commit_sha.lower()


def index_repo(
    repo_path: str,
    repo_name: str,
    *,
    scm_provider: str = "github",
    scm_base_url: str = "https://github.com",
    default_branch: str | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    if not REPOSITORY_NAME_PATTERN.fullmatch(repo_name) or any(
        part in {".", ".."} for part in repo_name.split("/")
    ):
        raise ValueError("repo_name must look like owner/repository")

    commit_sha = repository_commit(root)

    with closing(get_conn()) as conn:
        with conn:
            handle = begin_index_snapshot(
                conn,
                repo_name,
                commit_sha,
                scm_provider=scm_provider,
                scm_base_url=scm_base_url,
                default_branch=default_branch,
            )

        if progress_callback:
            progress_callback()
        if handle.state == "active":
            print(f"Index for {repo_name}@{commit_sha[:12]} is already active.")
            return
        if handle.state == "building":
            raise RuntimeError(f"Index for {repo_name}@{commit_sha[:12]} is already being built")

        try:
            print("Extracting code graph ...")
            graph = extract_repository_graph(root)
            print(
                f"  {len(graph.symbols)} symbols and {len(graph.relationships)} relationships found"
            )
            if graph.diagnostics:
                print(f"  {len(graph.diagnostics)} files could not be parsed")

            print("Discovering repository review policy ...")
            policy = discover_repository_policy(root)
            print(
                f"  {len(policy.layers)} config layers and "
                f"{len(policy.guidance_documents)} guidance documents found"
            )
            if policy.skipped_sources:
                print(
                    "  skipped non-regular guidance sources: "
                    + ", ".join(policy.skipped_sources)
                )

            print(f"Chunking {repo_name}@{commit_sha[:12]} ...")
            chunks = chunk_repo(root, symbols=graph.symbols)
            print(f"  {len(chunks)} chunks found")
            print("Indexing repository files for literal grep ...")
            files = index_repository_files(root)
            print(f"  {len(files)} searchable files found")
            if progress_callback:
                progress_callback()

            with conn:
                touch_snapshot(conn, handle.snapshot_id)
                existing = get_existing_hashes(conn, handle.previous_snapshot_id)
                existing_files = get_existing_file_hashes(conn, handle.previous_snapshot_id)
            changed = [
                chunk
                for chunk in chunks
                if existing.get((chunk.file_path, chunk.start_line, chunk.end_line))
                != chunk.content_hash
            ]
            unchanged_keys = [
                (chunk.file_path, chunk.start_line, chunk.end_line)
                for chunk in chunks
                if existing.get((chunk.file_path, chunk.start_line, chunk.end_line))
                == chunk.content_hash
            ]
            changed_files = [
                file
                for file in files
                if existing_files.get(file.file_path) != file.content_hash
            ]
            unchanged_file_paths = [
                file.file_path
                for file in files
                if existing_files.get(file.file_path) == file.content_hash
            ]

            print(
                f"  {len(changed)} chunks changed/new; {len(unchanged_keys)} reusable; "
                f"{len(changed_files)} files changed/new; {len(unchanged_file_paths)} reusable"
            )
            if progress_callback:
                progress_callback()

            with conn:
                touch_snapshot(conn, handle.snapshot_id)
                copy_unchanged_chunks(
                    conn,
                    handle.previous_snapshot_id,
                    handle.snapshot_id,
                    unchanged_keys,
                )
                copy_unchanged_files(
                    conn,
                    handle.previous_snapshot_id,
                    handle.snapshot_id,
                    unchanged_file_paths,
                )
                upsert_chunks(conn, handle.snapshot_id, changed)
                upsert_repository_files(conn, handle.snapshot_id, changed_files)
                write_symbol_graph(
                    conn,
                    handle.snapshot_id,
                    list(graph.symbols),
                    list(graph.relationships),
                )
                write_repository_policy(conn, handle.snapshot_id, policy)
                validate_snapshot_ready(
                    conn,
                    handle.snapshot_id,
                    expected_chunks=len(chunks),
                    expected_symbols=len(graph.symbols),
                    expected_relationships=len(graph.relationships),
                    expected_policy_layers=len(policy.layers),
                    expected_guidance_documents=len(policy.guidance_documents),
                    expected_files=len(files),
                )
                activated = activate_snapshot(conn, handle.snapshot_id)
            if progress_callback:
                progress_callback()
        except BaseException:
            conn.rollback()
            with conn:
                mark_snapshot_failed(conn, handle.snapshot_id)
            raise

    state = "active" if activated else "superseded by a newer index request"
    print(
        f"Done. Snapshot {handle.snapshot_id} for {repo_name}@{commit_sha[:12]} is {state} "
        f"({len(chunks)} chunks, {len(changed)} written, {len(unchanged_keys)} reused; "
        f"{len(files)} grep files, {len(changed_files)} written, "
        f"{len(unchanged_file_paths)} reused; {len(graph.symbols)} symbols)."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-path", required=True, help="Path to a clean Git checkout")
    parser.add_argument("--repo-name", required=True, help="Repository identifier, e.g. owner/repo")
    parser.add_argument(
        "--scm-provider",
        choices=("github",),
        default="github",
    )
    parser.add_argument("--scm-base-url", default="https://github.com")
    parser.add_argument("--default-branch")
    args = parser.parse_args()
    try:
        index_repo(
            args.repo_path,
            args.repo_name,
            scm_provider=args.scm_provider,
            scm_base_url=args.scm_base_url,
            default_branch=args.default_branch,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
