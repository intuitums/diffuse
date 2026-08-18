from diffuse.review.provenance import (
    CommitMetadata,
    PullRequestCommits,
    PullRequestProvenance,
    classify_pull_request_provenance,
    model_family,
    select_review_agent_plan,
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


def test_direct_devin_agent_author_is_detected_without_guessing_its_model():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_login="devin-ai-integration[bot]",
                    author_type="Bot",
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
    assert provenance.tool == "devin"
    assert provenance.confidence == 0.98
    assert provenance.ai_commit_ratio == 1


def test_merged_claude_and_devin_history_is_classified_as_ambiguous():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    message=(
                        "Fix review retries\n\n"
                        "Co-authored-by: Claude <noreply@anthropic.com>\n"
                        "Co-authored-by: Devin"
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
            "committer_name": "Claude",
            "committer_email": "noreply@anthropic.com",
            "verified": True,
        },
        "codex_verified": {
            "committer_name": "OpenAI Codex",
            "committer_email": "noreply@openai.com",
            "verified": True,
        },
        "gemini_bot": {"author_login": "gemini-code-assist[bot]", "author_type": "Bot"},
        "ai_assisted_trailer": {
            "message": "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>"
        },
        "mixed_weak_ai": {
            "message": (
                "Fix\n\nMade-with: Claude\n"
                "Co-authored-by: Codex <noreply@openai.com>"
            ),
        },
        "unknown_family_agent": {
            "author_login": "devin-ai-integration[bot]",
            "author_type": "Bot",
        },
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


def test_a_forged_git_author_email_cannot_select_the_reviewer():
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


def test_a_verified_committer_does_not_verify_a_forged_author_email():
    """A valid signer must not authenticate a separately chosen Git author."""

    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_name="Claude",
                    author_email="noreply@anthropic.com",
                    committer_name="Attacker",
                    committer_email="attacker@example.com",
                    verified=True,
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
                    committer_name="Claude",
                    committer_email="noreply@anthropic.com",
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


def test_a_forged_trailer_cannot_veto_a_provider_asserted_family():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_login="openai-codex[bot]",
                    author_type="Bot",
                    message="Add review routing\n\nMade-with: Claude",
                ),
            ),
            complete=True,
        )
    )

    plan = select_review_model_plan(
        provenance,
        candidate_model="openai/gpt-4.1-mini",
        verifier_model="anthropic/claude-sonnet-4.6",
    )

    assert provenance.classification == "agent_authored"
    assert provenance.model_family == "openai"
    assert provenance.signals == (
        "commit_author:codex",
        "commit_trailer_made_with:claude_code",
    )
    assert plan.reason_code == "opposing_openai_reviewer"
    assert plan.candidate_model == "anthropic/claude-sonnet-4.6"


def test_conflicting_provider_asserted_families_remain_ambiguous():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    sha="a" * 40,
                    author_login="openai-codex[bot]",
                    author_type="Bot",
                ),
                _commit(
                    sha="b" * 40,
                    author_login="claude[bot]",
                    author_type="Bot",
                ),
            ),
            complete=True,
        )
    )

    assert provenance.classification == "mixed_ai"
    assert provenance.model_family is None
    assert provenance.confidence == 0.98


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


def test_unknown_devin_origin_uses_cross_family_candidate_and_verifier():
    provenance = classify_pull_request_provenance(
        PullRequestCommits(
            commits=(
                _commit(
                    author_login="devin-ai-integration[bot]",
                    author_type="Bot",
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


def test_cli_runner_routing_defaults_to_codex_and_opposes_trusted_families():
    def provenance(family: str | None, confidence: float = 0.0) -> PullRequestProvenance:
        return PullRequestProvenance(
            classification="agent_authored" if family else "human_or_undetected",
            model_family=family,
            tool="codex" if family == "openai" else "claude_code" if family else None,
            confidence=confidence,
            commit_count=1,
            ai_commit_count=1 if family else 0,
            metadata_complete=True,
        )

    assert select_review_agent_plan(provenance(None)).runtime == "codex"
    assert select_review_agent_plan(provenance("openai", 0.98)).runtime == "claude"
    assert select_review_agent_plan(provenance("anthropic", 0.98)).runtime == "codex"
    # Weak or forgeable evidence cannot influence selection.
    assert select_review_agent_plan(provenance("openai", 0.7)).runtime == "codex"
