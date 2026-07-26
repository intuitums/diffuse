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
    # A trailer is part of the commit message and carries no signature, so it is
    # recorded as evidence but capped below the routing threshold.
    assert provenance.confidence == 0.7
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
    # Unverified Git author email; see UNVERIFIED_IDENTITY_MAX_STRENGTH.
    assert provenance.confidence == 0.7
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


def test_anthropic_origin_generates_with_openai_and_keeps_an_independent_verifier():
    """Routing swaps which model generates; it must not drop the second opinion.

    Assigning the opposing model to both stages would make the model that
    proposes findings the same one that verifies them, on exactly the changes
    this feature exists to scrutinise.
    """

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(_commit(author_login="claude[bot]", author_type="Bot"),),
            complete=True,
        ),
        pull_request_author="claude[bot]",
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openrouter/anthropic/claude-sonnet-4.6",
        verifier_model="openrouter/openai/gpt-5.2",
    )

    assert plan.candidate_model == "openrouter/openai/gpt-5.2"
    assert plan.verifier_model == "openrouter/anthropic/claude-sonnet-4.6"
    assert plan.reason_code == "opposing_anthropic_reviewer"


def test_provenance_routing_is_always_a_permutation_of_the_configured_pair():
    """No classification may contract the configured pair onto one model."""

    configured = ("openrouter/anthropic/claude-sonnet-4.6", "openrouter/openai/gpt-5.2")
    commit_variants = {
        "agent_authored_bot": {"author_login": "claude[bot]", "author_type": "Bot"},
        "agent_authored_verified": {
            "author_email": "noreply@anthropic.com",
            "verified": True,
        },
        "codex_verified": {"author_email": "noreply@openai.com", "verified": True},
        "gemini_bot": {"author_login": "gemini-code-assist[bot]", "author_type": "Bot"},
        "ai_assisted_trailer": {
            "message": "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>"
        },
        "mixed_ai": {
            "author_login": "claude[bot]",
            "author_type": "Bot",
            "message": "Fix\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>",
        },
        "unknown_family_agent": {"author_email": "cursoragent@cursor.com"},
        "human": {},
    }

    for label, overrides in commit_variants.items():
        for complete in (True, False):
            provenance = classify_pull_request_provenance(
                PullRequestCommits(
                    commits=(_commit(**overrides),), complete=complete
                )
            )
            plan = select_review_model_plan(
                provenance,
                candidate_model=configured[0],
                verifier_model=configured[1],
            )
            assert sorted((plan.candidate_model, plan.verifier_model)) == sorted(
                configured
            ), f"{label} (complete={complete}) collapsed onto one model"


def test_a_single_configured_model_cannot_produce_an_independent_verifier():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(_commit(author_login="claude[bot]", author_type="Bot"),),
            complete=True,
        ),
        pull_request_author="claude[bot]",
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="anthropic/claude-sonnet-4.6",
        verifier_model="anthropic/claude-sonnet-4.6",
    )

    assert plan.reason_code == "opposing_model_unavailable"
    assert plan.candidate_model == plan.verifier_model == "anthropic/claude-sonnet-4.6"


def test_a_forged_git_author_email_cannot_select_the_reviewer(monkeypatch):
    """`git commit --author=` must not let a PR author pick its own reviewer."""

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Claude",
                    author_email="noreply@anthropic.com",
                    committer_name="Claude",
                    committer_email="noreply@anthropic.com",
                    verified=False,
                ),
            ),
            complete=True,
        ),
        pull_request_author="attacker-human",
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="anthropic/claude-sonnet-4.6",
        verifier_model="openai/gpt-4.1-mini",
    )

    assert provenance.confidence == 0.7
    assert plan.reason_code == "low_confidence_cross_review"
    assert plan.candidate_model == "anthropic/claude-sonnet-4.6"
    assert plan.verifier_model == "openai/gpt-4.1-mini"


def test_a_forged_co_authored_by_trailer_cannot_select_the_reviewer():
    """The trailer path must be capped too, or it is a bypass for the above."""

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Add a backdoor\n\n"
                        "Co-Authored-By: Claude <noreply@anthropic.com>"
                    ),
                    author_email="attacker@example.com",
                ),
            ),
            complete=True,
        ),
        pull_request_author="attacker-human",
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="anthropic/claude-sonnet-4.6",
        verifier_model="openai/gpt-4.1-mini",
    )

    assert provenance.confidence == 0.7
    assert plan.reason_code == "low_confidence_cross_review"


def test_a_verified_agent_signature_still_routes():
    """Capping unverified identity must not disable the feature entirely."""

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Claude",
                    author_email="noreply@anthropic.com",
                    verified=True,
                ),
            ),
            complete=True,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="anthropic/claude-sonnet-4.6",
        verifier_model="openai/gpt-4.1-mini",
    )

    assert provenance.confidence == 0.92
    assert plan.reason_code == "opposing_anthropic_reviewer"
    assert plan.candidate_model == "openai/gpt-4.1-mini"
    assert plan.verifier_model == "anthropic/claude-sonnet-4.6"


def test_a_human_whose_name_collides_with_a_tool_is_not_ai_attribution():
    """"Claude Dubois" is a person, not an agent."""

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Fix pagination\n\n"
                        "Co-authored-by: Claude Dubois <claude.dubois@example.com>"
                    )
                ),
            ),
            complete=True,
        )
    )

    assert provenance.classification == "human_or_undetected"
    assert provenance.model_family is None
    assert provenance.confidence == 0
    assert provenance.ai_commit_count == 0


def test_a_product_named_trailer_without_an_email_is_still_detected():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(_commit(message="Fix pagination\n\nMade-with: Claude Code"),),
            complete=True,
        )
    )

    assert provenance.model_family == "anthropic"
    assert provenance.tool == "claude_code"
    assert provenance.confidence == 0.7


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

    assert provenance.confidence == 0.525
    assert plan.reason_code == "low_confidence_cross_review"
    assert plan.candidate_model == "openrouter/anthropic/claude-sonnet-4.6"
    assert plan.verifier_model == "openrouter/openai/gpt-5.2"


def test_model_family_understands_direct_and_openrouter_identifiers():
    assert model_family("anthropic/claude-sonnet-4-6") == "anthropic"
    assert model_family("openrouter/anthropic/claude-sonnet-4.6") == "anthropic"
    assert model_family("openrouter/openai/gpt-5.2") == "openai"
    assert model_family("google/gemini-3-pro") == "google"
    assert model_family("ollama/qwen3-coder") is None
