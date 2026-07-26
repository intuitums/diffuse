from service.review_provenance import (
    CommitMetadata,
    PullRequestCommits,
    classify_pull_request_provenance,
    model_family,
    select_review_model_plan,
)


def _commit(**overrides) -> CommitMetadata:
    values = {
        "sha": "a" * 40,
        "message": "Implement tenant checks",
        "author_name": "Fischer",
        "author_email": "developer@example.com",
        "committer_name": "Fischer",
        "committer_email": "developer@example.com",
    }
    values.update(overrides)
    return CommitMetadata(**values)


def test_claude_coauthor_trailer_from_repository_history_is_detected():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Implement tenant checks\n\n"
                        "Co-Authored-By: Claude <noreply@anthropic.com>"
                    )
                ),
            ),
            complete=True,
        ),
        pull_request_author="fschrhunt",
    )

    assert provenance.classification == "ai_assisted"
    assert provenance.model_family == "anthropic"
    assert provenance.tool == "claude_code"
    assert provenance.confidence == 0.9
    assert provenance.ai_commit_count == 1
    assert provenance.signals == ("commit_trailer_co_authored_by:claude_code",)


def test_direct_cursor_agent_author_is_detected_without_guessing_its_model():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Cursor Agent",
                    author_email="cursoragent@cursor.com",
                    committer_name="Cursor Agent",
                    committer_email="cursoragent@cursor.com",
                    message=(
                        "Fix review retries\n\n"
                        "Co-authored-by: Fischer <developer@example.com>"
                    ),
                ),
            ),
            complete=True,
        )
    )

    assert provenance.classification == "agent_unknown_family"
    assert provenance.model_family is None
    assert provenance.tool == "cursor"
    assert provenance.confidence == 0.92
    assert provenance.ai_commit_ratio == 1


def test_merged_claude_and_cursor_history_is_classified_as_ambiguous():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Fix review retries\n\n"
                        "Co-authored-by: Claude <noreply@anthropic.com>\n"
                        "Co-authored-by: Cursor Agent <cursoragent@cursor.com>"
                    )
                ),
            ),
            complete=True,
        )
    )

    assert provenance.classification == "mixed_ai"
    assert provenance.model_family is None
    assert provenance.tool is None


def test_checkpointer_and_incidental_agent_words_are_not_ai_attribution():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Checkpointer",
                    author_email="checkpointer@noreply",
                    committer_name="Checkpointer",
                    committer_email="checkpointer@noreply",
                    message="checkpoint: agent reviewed the workflow",
                ),
            ),
            complete=True,
        )
    )

    assert provenance.classification == "human_or_undetected"
    assert provenance.confidence == 0
    assert provenance.ai_commit_count == 0


def test_freely_chosen_agent_name_alone_cannot_select_a_model_family():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="OpenAI Codex",
                    author_email="unrelated@example.com",
                ),
            ),
            complete=True,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openrouter/openai/gpt-5.2",
        verifier_model="openrouter/anthropic/claude-sonnet-4.6",
    )

    assert provenance.model_family == "openai"
    assert provenance.confidence == 0.7
    assert plan.reason_code == "low_confidence_cross_review"
    assert plan.candidate_model == "openrouter/openai/gpt-5.2"
    assert plan.verifier_model == "openrouter/anthropic/claude-sonnet-4.6"


def test_anthropic_origin_routes_all_review_calls_to_configured_openai_model():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Implement tenant checks\n\n"
                        "Co-authored-by: Claude <noreply@anthropic.com>"
                    )
                ),
            ),
            complete=True,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openrouter/anthropic/claude-sonnet-4.6",
        verifier_model="openrouter/openai/gpt-5.2",
    )

    assert plan.candidate_model == "openrouter/openai/gpt-5.2"
    assert plan.verifier_model == "openrouter/openai/gpt-5.2"
    assert plan.reason_code == "opposing_anthropic_reviewer"


def test_unknown_cursor_origin_uses_cross_family_candidate_and_verifier():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Cursor Agent",
                    author_email="cursoragent@cursor.com",
                ),
            ),
            complete=True,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openrouter/anthropic/claude-sonnet-4.6",
        verifier_model="openrouter/openai/gpt-5.2",
    )

    assert plan.candidate_model == "openrouter/anthropic/claude-sonnet-4.6"
    assert plan.verifier_model == "openrouter/openai/gpt-5.2"
    assert plan.reason_code == "ambiguous_agent_cross_review"


def test_incomplete_metadata_cannot_force_opposing_model_routing():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Implement tenant checks\n\n"
                        "Co-authored-by: Claude <noreply@anthropic.com>"
                    )
                ),
            ),
            complete=False,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openrouter/anthropic/claude-sonnet-4.6",
        verifier_model="openrouter/openai/gpt-5.2",
    )

    assert provenance.confidence == 0.675
    assert plan.reason_code == "low_confidence_cross_review"
    assert plan.candidate_model == "openrouter/anthropic/claude-sonnet-4.6"
    assert plan.verifier_model == "openrouter/openai/gpt-5.2"


def test_model_family_understands_direct_and_openrouter_identifiers():
    assert model_family("anthropic/claude-sonnet-4-6") == "anthropic"
    assert model_family("openrouter/anthropic/claude-sonnet-4.6") == "anthropic"
    assert model_family("openrouter/openai/gpt-5.2") == "openai"
    assert model_family("google/gemini-3-pro") == "google"
    assert model_family("ollama/qwen3-coder") is None
