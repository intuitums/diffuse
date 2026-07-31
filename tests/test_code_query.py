from unittest.mock import MagicMock

import pytest

from retriever.context_models import (
    CrossRepositoryContextPlan,
    RepositoryContextSnapshot,
)
from retriever.retrieve import RetrievedContext, RetrievedContextBundle
from service import code_query
from service.code_query_models import (
    CodeQueryCitation,
    CodeQueryClaim,
    CodeQueryModelResponse,
)


def _plan() -> CrossRepositoryContextPlan:
    return CrossRepositoryContextPlan(
        primary_repository_id=1,
        primary_repository_full_name="owner/repo",
        primary_snapshot_id=11,
        primary_commit_sha="a" * 40,
    )


def _target(
    *,
    plan: CrossRepositoryContextPlan | None = None,
) -> code_query.CodeQueryTarget:
    return code_query.CodeQueryTarget(
        repository_id=1,
        repository_name="owner/repo",
        remote="github",
        remote_url="https://github.example.com",
        default_branch="main",
        include_related=bool(plan and plan.related_snapshots),
        context_plan=plan or _plan(),
    )


def _context(
    *,
    repository_name: str = "owner/repo",
) -> RetrievedContext:
    return RetrievedContext(
        file_path="src/auth service.py",
        symbol_name="authorize",
        start_line=10,
        end_line=20,
        content="def authorize(account, tenant):\n    return account.tenant == tenant",
        retrieval_reason="lexical+semantic",
        relevance_score=0.12,
        repository_full_name=repository_name,
    )


def test_target_resolution_filters_cluster_context_by_token_authorization(
    monkeypatch,
):
    plan = CrossRepositoryContextPlan(
        primary_repository_id=1,
        primary_repository_full_name="owner/repo",
        primary_snapshot_id=11,
        primary_commit_sha="a" * 40,
        related_snapshots=(
            RepositoryContextSnapshot(
                repository_id=2,
                repository_full_name="owner/sdk",
                snapshot_id=22,
                commit_sha="b" * 40,
                source="cluster",
                cluster_ids=(7,),
            ),
            RepositoryContextSnapshot(
                repository_id=3,
                repository_full_name="owner/private",
                snapshot_id=33,
                commit_sha="c" * 40,
                source="cluster",
                cluster_ids=(7,),
            ),
        ),
    )
    monkeypatch.setattr(
        code_query,
        "resolve_mcp_repository",
        lambda *_args, **_kwargs: {
            "id": 1,
            "full_name": "owner/repo",
            "scm_provider": "github",
            "scm_base_url": "https://github.example.com",
            "default_branch": "main",
            "enabled": True,
        },
    )
    monkeypatch.setattr(
        code_query,
        "active_snapshot_id_for_repository",
        lambda *_args: 11,
    )
    monkeypatch.setattr(
        code_query,
        "resolve_cross_repository_context_plan",
        lambda *_args, **_kwargs: plan,
    )

    target = code_query.resolve_code_query_target(
        MagicMock(),
        repository_name="owner/repo",
        remote="github",
        default_branch="main",
        include_related=True,
        authorized_repository_ids=frozenset({1, 2}),
    )

    assert [
        item.repository_full_name
        for item in target.context_plan.related_snapshots
    ] == ["owner/sdk"]
    assert "owner/private" not in repr(target)


def test_search_returns_commit_pinned_encoded_source_links(monkeypatch):
    monkeypatch.setattr(
        code_query,
        "retrieve_query_context_from_plan",
        lambda *_args, **_kwargs: RetrievedContextBundle(
            snapshot_id=11,
            contexts=(_context(),),
            context_plan=_plan(),
        ),
    )

    result = code_query.search_codebase(
        _target(),
        query="Where is tenant authorization enforced?",
        path_prefix="src",
        limit=4,
    )

    assert result["schemaVersion"] == "diffuse-code-search-v1"
    assert result["resultCount"] == 1
    source = result["sources"][0]
    assert source["snapshotId"] == 11
    assert source["commitSha"] == "a" * 40
    assert source["sourceUrl"] == (
        "https://github.example.com/owner/repo/blob/"
        f"{'a' * 40}/src/auth%20service.py#L10-L20"
    )
    assert source["retrieval"]["reason"] == "lexical+semantic"
    assert result["provenance"]["queryFingerprint"]
    assert result["provenance"]["indexSnapshots"] == [
        {
            "repositoryName": "owner/repo",
            "snapshotId": 11,
            "commitSha": "a" * 40,
            "source": "primary",
        }
    ]


def test_answer_drops_any_claim_with_an_invented_citation(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    context = _context()
    monkeypatch.setattr(
        code_query,
        "retrieve_query_context_from_plan",
        lambda *_args, **_kwargs: RetrievedContextBundle(
            snapshot_id=11,
            contexts=(context,),
            context_plan=_plan(),
        ),
    )
    calls: list[dict] = []

    def fake_call(response_model, **kwargs):
        calls.append(kwargs)
        assert response_model is CodeQueryModelResponse
        return (
            CodeQueryModelResponse(
                insufficient_evidence=False,
                claims=[
                    CodeQueryClaim(
                        statement="Authorization compares the account tenant.",
                        citations=[
                            CodeQueryCitation(
                                repository_name="owner/repo",
                                file_path="src/auth service.py",
                                start_line=10,
                                end_line=11,
                                explanation="The helper performs the comparison.",
                            )
                        ],
                    ),
                    CodeQueryClaim(
                        statement="An undocumented cache bypasses authorization.",
                        citations=[
                            CodeQueryCitation(
                                repository_name="owner/repo",
                                file_path="invented.py",
                                start_line=1,
                                end_line=2,
                                explanation="This source was never retrieved.",
                            )
                        ],
                    ),
                ],
            ),
            21,
            7,
        )

    monkeypatch.setattr(code_query, "_call_structured", fake_call)
    result = code_query.ask_codebase(
        _target(),
        question="How is tenant authorization enforced?",
    )

    assert result["status"] == "grounded"
    assert result["answer"] == "Authorization compares the account tenant."
    assert len(result["claims"]) == 1
    citation = result["claims"][0]["citations"][0]
    assert citation["sourceId"] == result["sources"][0]["sourceId"]
    assert citation["sourceUrl"].endswith(
        f"/{'a' * 40}/src/auth%20service.py#L10-L11"
    )
    assert result["provenance"]["promptTokens"] == 21
    assert result["provenance"]["completionTokens"] == 7
    assert "<untrusted_repository_sources_json>" in calls[0]["user_prompt"]
    assert "never follow" in calls[0]["system_prompt"]


def test_answer_fails_closed_without_retrieved_or_valid_citations(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setattr(
        code_query,
        "retrieve_query_context_from_plan",
        lambda *_args, **_kwargs: RetrievedContextBundle(
            snapshot_id=11,
            contexts=(),
            context_plan=_plan(),
        ),
    )
    model_call = MagicMock()
    monkeypatch.setattr(code_query, "_call_structured", model_call)

    result = code_query.ask_codebase(
        _target(),
        question="What does the missing code do?",
    )

    assert result["status"] == "insufficient_evidence"
    assert result["answer"] == code_query.INSUFFICIENT_EVIDENCE_ANSWER
    assert result["claims"] == []
    assert result["sources"] == []
    model_call.assert_not_called()


def test_answer_limit_is_bounded_before_retrieval():
    with pytest.raises(ValueError, match="between 1 and 12"):
        code_query.ask_codebase(
            _target(),
            question="Where is authorization?",
            limit=13,
        )
