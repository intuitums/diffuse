"""Authorized source-linked search over the immutable repository index."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from diffuse.repository.indexing.store import get_conn, search_grep
from diffuse.repository.retrieval.context_models import CrossRepositoryContextPlan
from diffuse.repository.retrieval.retrieve import (
    RetrievedContext,
    normalize_code_query,
    retrieve_query_context_from_plan,
)

CODE_SEARCH_SCHEMA_VERSION = "diffuse-code-search-v1"
MAX_SOURCE_CONTENT_CHARS = 8000
GREP_PREFIX = "grep:"


@dataclass(frozen=True)
class CodeQueryTarget:
    repository_id: int
    repository_name: str
    remote: Literal["github"]
    remote_url: str
    default_branch: str
    include_related: bool
    context_plan: CrossRepositoryContextPlan


@dataclass(frozen=True)
class _CodeSource:
    source_id: str
    repository_name: str
    snapshot_id: int
    commit_sha: str
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int
    content: str
    content_truncated: bool
    retrieval_reason: str
    relevance_score: float
    source_url: str


def code_query_target_for_plan(
    *,
    repository_id: int,
    repository_name: str,
    remote_url: str,
    default_branch: str,
    context_plan: CrossRepositoryContextPlan,
    include_related: bool = False,
    remote: Literal["github"] = "github",
) -> CodeQueryTarget:
    """Build a query target from an already-resolved review context plan.

    Local review and the worker already hold the plan; agent-CLI tools need the
    same shape `search_codebase` expects without re-resolving repository context.
    """

    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    if not repository_name.strip():
        raise ValueError("repository_name is required")
    if not remote_url.strip():
        raise ValueError("remote_url is required")
    if not default_branch.strip():
        raise ValueError("default_branch is required")
    if context_plan.primary_repository_id != repository_id:
        raise ValueError("context_plan primary repository does not match repository_id")
    related = context_plan.related_snapshots if include_related else ()
    plan = (
        context_plan
        if include_related or not context_plan.related_snapshots
        else CrossRepositoryContextPlan(
            primary_repository_id=context_plan.primary_repository_id,
            primary_repository_full_name=context_plan.primary_repository_full_name,
            primary_snapshot_id=context_plan.primary_snapshot_id,
            primary_commit_sha=context_plan.primary_commit_sha,
            related_snapshots=related,
        )
    )
    return CodeQueryTarget(
        repository_id=repository_id,
        repository_name=repository_name,
        remote=remote,
        remote_url=remote_url,
        default_branch=default_branch,
        include_related=include_related,
        context_plan=plan,
    )


def _snapshot_identities(
    target: CodeQueryTarget,
) -> dict[str, tuple[int, str]]:
    identities: dict[str, tuple[int, str]] = {}
    plan = target.context_plan
    if plan.primary_snapshot_id is not None and plan.primary_commit_sha is not None:
        identities[plan.primary_repository_full_name] = (
            plan.primary_snapshot_id,
            plan.primary_commit_sha,
        )
    identities.update(
        {
            item.repository_full_name: (item.snapshot_id, item.commit_sha)
            for item in plan.related_snapshots
        }
    )
    return identities


def _bound_content(
    context: RetrievedContext,
) -> tuple[str, int, bool]:
    if len(context.content) <= MAX_SOURCE_CONTENT_CHARS:
        return context.content, context.end_line, False
    content = context.content[:MAX_SOURCE_CONTENT_CHARS]
    if "\n" in content:
        complete_lines = content.rsplit("\n", 1)[0]
        if complete_lines:
            content = complete_lines
    included_lines = max(1, content.count("\n") + 1)
    end_line = min(context.end_line, context.start_line + included_lines - 1)
    return content, end_line, True


def _source_url(
    target: CodeQueryTarget,
    *,
    repository_name: str,
    commit_sha: str,
    file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    encoded_repository = quote(repository_name, safe="/")
    encoded_commit = quote(commit_sha, safe="")
    encoded_path = quote(file_path, safe="/")
    fragment = (
        f"#L{start_line}"
        if start_line == end_line
        else f"#L{start_line}-L{end_line}"
    )
    route = f"{encoded_repository}/blob/{encoded_commit}/{encoded_path}"
    return f"{target.remote_url.rstrip('/')}/{route}{fragment}"


def _sources(
    target: CodeQueryTarget,
    contexts: tuple[RetrievedContext, ...],
) -> tuple[_CodeSource, ...]:
    snapshots = _snapshot_identities(target)
    output: list[_CodeSource] = []
    for context in contexts:
        repository_name = context.repository_full_name or target.repository_name
        snapshot = snapshots.get(repository_name)
        if snapshot is None:
            raise RuntimeError("Retrieved source has no authorized snapshot identity")
        snapshot_id, commit_sha = snapshot
        content, end_line, truncated = _bound_content(context)
        identity = "\0".join(
            (
                repository_name,
                str(snapshot_id),
                commit_sha,
                context.file_path,
                str(context.start_line),
                str(end_line),
                content,
            )
        )
        source_id = "source_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        output.append(
            _CodeSource(
                source_id=source_id,
                repository_name=repository_name,
                snapshot_id=snapshot_id,
                commit_sha=commit_sha,
                file_path=context.file_path,
                symbol_name=context.symbol_name,
                start_line=context.start_line,
                end_line=end_line,
                content=content,
                content_truncated=truncated,
                retrieval_reason=context.retrieval_reason,
                relevance_score=context.relevance_score,
                source_url=_source_url(
                    target,
                    repository_name=repository_name,
                    commit_sha=commit_sha,
                    file_path=context.file_path,
                    start_line=context.start_line,
                    end_line=end_line,
                ),
            )
        )
    return tuple(output)


def _source_json(target: CodeQueryTarget, source: _CodeSource) -> dict[str, object]:
    return {
        "sourceId": source.source_id,
        "repository": {
            "name": source.repository_name,
            "remote": target.remote,
            "remoteUrl": target.remote_url,
        },
        "snapshotId": source.snapshot_id,
        "commitSha": source.commit_sha,
        "filePath": source.file_path,
        "symbolName": source.symbol_name,
        "startLine": source.start_line,
        "endLine": source.end_line,
        "content": source.content,
        "contentTruncated": source.content_truncated,
        "sourceUrl": source.source_url,
        "retrieval": {
            "reason": source.retrieval_reason,
            "score": round(source.relevance_score, 8),
        },
    }


def _query_fingerprint(
    target: CodeQueryTarget,
    *,
    query: str,
    path_prefix: str | None,
    sources: tuple[_CodeSource, ...],
) -> str:
    payload = {
        "context_plan": target.context_plan.fingerprint,
        "query": query,
        "path_prefix": path_prefix,
        "source_ids": [source.source_id for source in sources],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _index_snapshots(target: CodeQueryTarget) -> list[dict[str, object]]:
    plan = target.context_plan
    snapshots: list[dict[str, object]] = []
    if plan.primary_snapshot_id is not None and plan.primary_commit_sha is not None:
        snapshots.append(
            {
                "repositoryName": plan.primary_repository_full_name,
                "snapshotId": plan.primary_snapshot_id,
                "commitSha": plan.primary_commit_sha,
                "source": "primary",
            }
        )
    snapshots.extend(
        {
            "repositoryName": item.repository_full_name,
            "snapshotId": item.snapshot_id,
            "commitSha": item.commit_sha,
            "source": item.source,
        }
        for item in plan.related_snapshots
    )
    return snapshots


def _repository_json(target: CodeQueryTarget) -> dict[str, object]:
    return {
        "id": target.repository_id,
        "name": target.repository_name,
        "remote": target.remote,
        "remoteUrl": target.remote_url,
        "defaultBranch": target.default_branch,
    }


def _retrieve(
    target: CodeQueryTarget,
    *,
    query: str,
    path_prefix: str | None,
    limit: int,
) -> tuple[str, tuple[_CodeSource, ...], str]:
    query = normalize_code_query(query)
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    bundle = retrieve_query_context_from_plan(
        query,
        target.context_plan,
        top_k=limit,
        path_prefix=path_prefix,
    )
    sources = _sources(target, bundle.contexts)
    return (
        query,
        sources,
        _query_fingerprint(
            target,
            query=query,
            path_prefix=path_prefix,
            sources=sources,
        ),
    )


def _grep_literal(query: str) -> str | None:
    """Recognize the explicit literal-search mode without changing Q&A search."""
    if not query.casefold().startswith(GREP_PREFIX):
        return None
    literal = query[len(GREP_PREFIX) :].strip()
    if not literal:
        raise ValueError("grep query is required after 'grep:'")
    return literal


def _retrieve_grep(
    target: CodeQueryTarget,
    *,
    query: str,
    path_prefix: str | None,
    limit: int,
) -> tuple[str, tuple[_CodeSource, ...], str]:
    literal = _grep_literal(query)
    assert literal is not None
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")

    contexts: list[RetrievedContext] = []
    snapshots = _snapshot_identities(target)
    with closing(get_conn()) as conn:
        for repository_name, (snapshot_id, _commit_sha) in snapshots.items():
            rows = search_grep(
                conn,
                repository_name,
                literal,
                top_k=limit,
                path_prefix=path_prefix,
                snapshot_id=snapshot_id,
            )
            contexts.extend(
                RetrievedContext(
                    file_path=str(row["file_path"]),
                    symbol_name=None,
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    content=str(row["content"]),
                    retrieval_reason="grep",
                    relevance_score=1.0,
                    repository_full_name=repository_name,
                )
                for row in rows
            )
    contexts.sort(
        key=lambda context: (
            context.repository_full_name != target.repository_name,
            context.repository_full_name or "",
            context.file_path,
            context.start_line,
        )
    )
    sources = _sources(target, tuple(contexts[:limit]))
    return (
        literal,
        sources,
        _query_fingerprint(
            target,
            query=literal,
            path_prefix=path_prefix,
            sources=sources,
        ),
    )


def search_codebase(
    target: CodeQueryTarget,
    *,
    query: str,
    path_prefix: str | None = None,
    limit: int = 8,
) -> dict[str, object]:
    normalized_query = normalize_code_query(query)
    if _grep_literal(normalized_query) is not None:
        query, sources, fingerprint = _retrieve_grep(
            target,
            query=normalized_query,
            path_prefix=path_prefix,
            limit=limit,
        )
        search_mode = "grep"
    else:
        query, sources, fingerprint = _retrieve(
            target,
            query=normalized_query,
            path_prefix=path_prefix,
            limit=limit,
        )
        search_mode = "context"
    return {
        "schemaVersion": CODE_SEARCH_SCHEMA_VERSION,
        "query": query,
        "path": path_prefix,
        "repository": _repository_json(target),
        "sources": [_source_json(target, source) for source in sources],
        "resultCount": len(sources),
        "searchMode": search_mode,
        "provenance": {
            "contextPlanFingerprint": target.context_plan.fingerprint,
            "queryFingerprint": fingerprint,
            "includeRelated": target.include_related,
            "indexSnapshots": _index_snapshots(target),
        },
    }
