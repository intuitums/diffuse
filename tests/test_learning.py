import pytest

from service import learning_engine
from service.models.learning import (
    RuleLearningEvidence,
    RuleLearningJobEvent,
    SuggestedRuleBatch,
    SuggestedRuleCandidate,
    candidate_deduplication_key,
)


def _evidence(event_id: int = 11) -> RuleLearningEvidence:
    return RuleLearningEvidence(
        event_id=event_id,
        pull_request_id=7,
        pull_request_number=31,
        source_kind="reply",
        signal_kind="context",
        content="Use the shared validator for every API payload.",
        finding_title="Validate the request body",
        finding_body="The handler consumes the payload without schema validation.",
        file_path="src/api/users.py",
        category="api",
        severity="high",
        suppression_protected=False,
    )


def test_rule_learning_job_identity_is_strict_and_round_trips():
    event = RuleLearningJobEvent(
        repository_id=3,
        repo_full_name="owner/repo",
        generation=2,
        evidence_fingerprint="a" * 64,
    )

    assert RuleLearningJobEvent.from_payload(event.to_payload()) == event
    assert event.scope_key == "repository:owner/repo:rule_learning"
    assert event.idempotency_key.endswith(f"generation:2:evidence:{'a' * 64}")
    with pytest.raises(ValueError, match="schema"):
        RuleLearningJobEvent.from_payload({**event.to_payload(), "extra": True})


def test_suggested_rule_deduplication_normalizes_case_and_whitespace():
    first = SuggestedRuleCandidate(
        title="Use Shared Validators",
        guidance="API handlers must use the shared schema validator.",
        applies_to=("src/api/**",),
        severity="high",
        category="api",
        evidence_event_ids=(1, 2, 3),
    )
    second = first.model_copy(
        update={
            "title": " use shared validators ",
            "guidance": "API handlers  MUST use the shared schema validator.",
        }
    )

    assert candidate_deduplication_key(first) == candidate_deduplication_key(second)


def test_learning_engine_requires_cited_repeated_rules_and_bounds_evidence(monkeypatch):
    suggestion = SuggestedRuleCandidate(
        title="Use the shared request validator",
        guidance="API handlers must validate payloads with the shared schema helper.",
        applies_to=("src/api/**",),
        severity="high",
        category="api",
        evidence_event_ids=(11,),
    )
    calls = []

    def fake_call(response_model, **kwargs):
        calls.append((response_model, kwargs))
        return SuggestedRuleBatch(suggestions=(suggestion,)), 12, 4

    monkeypatch.setenv("RULE_LEARNING_MODEL", "openai/test-learning")
    monkeypatch.setattr(learning_engine, "_call_structured", fake_call)
    large = _evidence().__class__(
        **{
            **_evidence().__dict__,
            "content": "x" * 5000,
        }
    )

    batch, prompt_tokens, completion_tokens = learning_engine.generate_suggested_rules(
        (large,),
        minimum_support=1,
        minimum_support_pull_requests=1,
    )

    assert batch.suggestions == (suggestion,)
    assert (prompt_tokens, completion_tokens) == (12, 4)
    assert calls[0][0] is SuggestedRuleBatch
    assert calls[0][1]["model_name"] == "openai/test-learning"
    assert "human must inspect and approve" in calls[0][1]["system_prompt"]
    assert "... truncated by Diffuse ..." in calls[0][1]["user_prompt"]
