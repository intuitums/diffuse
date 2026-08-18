"""Review tool provider: search_code over a pinned plan, always recorded."""

from __future__ import annotations

import pytest
from diffuse.repository.query import CODE_SEARCH_SCHEMA_VERSION, code_query_target_for_plan
from diffuse.repository.retrieval.context_models import CrossRepositoryContextPlan
from diffuse.review.tools import (
    SEARCH_CODE_TOOL,
    MemoryToolRecorder,
    ReviewToolProvider,
    build_review_tool_provider,
)


def _plan(**overrides) -> CrossRepositoryContextPlan:
    values = {
        "primary_repository_id": 7,
        "primary_repository_full_name": "owner/repo",
        "primary_snapshot_id": 11,
        "primary_commit_sha": "a" * 40,
        "related_snapshots": (),
    }
    values.update(overrides)
    return CrossRepositoryContextPlan(**values)


def _target(**overrides):
    plan = overrides.pop("context_plan", _plan())
    values = {
        "repository_id": 7,
        "repository_name": "owner/repo",
        "remote_url": "https://github.com",
        "default_branch": "main",
        "context_plan": plan,
    }
    values.update(overrides)
    return code_query_target_for_plan(**values)


def test_code_query_target_for_plan_requires_matching_primary():
    with pytest.raises(ValueError, match="does not match"):
        code_query_target_for_plan(
            repository_id=9,
            repository_name="owner/repo",
            remote_url="https://github.com",
            default_branch="main",
            context_plan=_plan(),
        )


def test_search_code_records_a_successful_call(monkeypatch):
    payload = {
        "schemaVersion": CODE_SEARCH_SCHEMA_VERSION,
        "query": "auth bypass",
        "path": None,
        "sources": [],
        "resultCount": 0,
        "provenance": {},
    }
    monkeypatch.setattr(
        "diffuse.review.tools.search_codebase",
        lambda target, **kwargs: payload,
    )
    recorder = MemoryToolRecorder()
    provider = build_review_tool_provider(_target(), recorder=recorder)

    result = provider.search_code("auth bypass", limit=4)

    assert result is payload
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.tool_name == SEARCH_CODE_TOOL
    assert call.arguments == {"query": "auth bypass", "path": None, "limit": 4}
    assert call.result is payload
    assert call.failure_code is None
    assert call.index_snapshot_ids == (11,)
    assert call.context_plan_fingerprint == _plan().fingerprint
    assert call.duration_ms >= 0


def test_search_code_records_a_failed_call_and_reraises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr("diffuse.review.tools.search_codebase", boom)
    recorder = MemoryToolRecorder()
    provider = ReviewToolProvider(_target(), recorder=recorder)

    with pytest.raises(RuntimeError, match="index unavailable"):
        provider.search_code("auth")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.tool_name == SEARCH_CODE_TOOL
    assert call.failure_code == "search_failed"
    assert "index unavailable" in (call.failure_detail or "")
    assert call.result is None
