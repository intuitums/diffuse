import pytest

from repository_policy.models import (
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
)
from repository_policy.resolve import resolve_review_policy
from service.auto_approval import (
    AutoApprovalRisk,
    assess_change_risk,
    evaluate_auto_approval,
)
from service.models.review import Category, ReviewFinding, ReviewReport, Severity
from service.scm import PullRequestEvent


def _diff(
    old_path: str = "docs/guide.md",
    new_path: str = "docs/guide.md",
) -> str:
    return (
        f"diff --git a/{old_path} b/{new_path}\n"
        f"--- a/{old_path}\n"
        f"+++ b/{new_path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _event(**overrides) -> PullRequestEvent:
    values = {
        "provider": "github",
        "scm_base_url": "https://github.com",
        "api_base_url": "https://api.github.com",
        "repo_full_name": "owner/repo",
        "number": 7,
        "web_url": "https://github.com/owner/repo/pull/7",
        "action": "opened",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "updated_at": "2026-07-23T15:30:00Z",
        "delivery_id": "delivery-7",
        "author": "octocat",
        "base_branch": "main",
        "head_branch": "docs",
        "is_draft": False,
        "labels": ("safe-change",),
        "title": "Clarify the guide",
        "description": "Documentation only.",
        "trigger_kind": "automatic",
        "trigger_id": "",
        "metadata_complete": True,
        "changed_file_count": 1,
    }
    values.update(overrides)
    return PullRequestEvent.from_payload(values)


def _report(**overrides) -> ReviewReport:
    values = {
        "summary": "No issues.",
        "risk_score": 0,
        "findings": [],
        "diff_file_count": 1,
        "reviewed_file_count": 1,
        "ignored_file_count": 0,
        "context_chunk_count": 1,
        "prompt_tokens": 10,
        "completion_tokens": 2,
    }
    values.update(overrides)
    return ReviewReport(**values)


def _policy(
    config: dict,
    paths: tuple[str, ...] = ("docs/guide.md",),
):
    return resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {"version": 1, **config}
                    ),
                ),
            )
        ),
        paths,
    )


def test_auto_approval_is_default_off_and_clean_low_risk_is_opt_in():
    diff = _diff()
    event = _event()
    report = _report()

    disabled = evaluate_auto_approval(
        resolve_review_policy(
            RepositoryPolicySnapshot(),
            ("docs/guide.md",),
        ),
        event,
        diff,
        report,
    )
    enabled = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "filters": {"allow_paths": ["docs/**"]},
                }
            }
        ),
        event,
        diff,
        report,
    )

    assert not disabled.eligible
    assert disabled.reason_code == "disabled"
    assert enabled.eligible
    assert enabled.reason_code == "approved"
    assert enabled.risk_level is AutoApprovalRisk.LOW
    assert enabled.risk_ceiling is AutoApprovalRisk.LOW


def test_an_enabled_repository_without_an_allowlist_approves_nothing():
    """Enabling the feature is a statement of intent, not a list of paths.

    The old gate approved anything Diffuse's built-in denylist failed to name, which made
    every path Diffuse never thought of -- `internal/perms/`, `lib/rbac/` -- eligible by
    accident. Now the operator names the paths, and naming none means approving none.
    """
    decision = evaluate_auto_approval(
        _policy({"auto_approval": {"enabled": True}}),
        _event(),
        _diff(),
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "path_not_allowlisted"


def test_an_allowlist_covers_only_the_paths_it_names():
    """A grant is per path, so an allowed file cannot carry an unallowed one in with it."""
    diff = _diff() + _diff("src/app.py", "src/app.py")

    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "filters": {"allow_paths": ["docs/**"]},
                }
            },
            ("docs/guide.md", "src/app.py"),
        ),
        _event(changed_file_count=2),
        diff,
        _report(diff_file_count=2, reviewed_file_count=2),
    )

    assert not decision.eligible
    assert decision.reason_code == "path_not_allowlisted"


def test_an_explicitly_empty_allowlist_withdraws_a_parent_grant():
    """`allow_paths: []` is a decision, not an omission, and it has to outrank a parent."""
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {
                            "enabled": True,
                            "filters": {"allow_paths": ["**"]},
                        },
                    }
                ),
            ),
            PolicyLayer(
                directory_path="docs",
                source_path="docs/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {"filters": {"allow_paths": []}},
                    }
                ),
            ),
        )
    )
    policy = resolve_review_policy(snapshot, ("docs/guide.md",))

    decision = evaluate_auto_approval(policy, _event(), _diff(), _report())

    assert not decision.eligible
    assert decision.reason_code == "path_not_allowlisted"


def test_omitting_allow_paths_is_the_same_decision_as_an_empty_list():
    """A parent that only enables auto-approval must not be widened by a nested grant.

    Docs and the field comment both say omit and `[]` mean the scope approves
    nothing. Skipping `None` during merge used to leave `allow_path_groups` holding
    only the nested `**`, so a root that never named a path still approved.
    """
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {"enabled": True},
                    }
                ),
            ),
            PolicyLayer(
                directory_path="src",
                source_path="src/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {"filters": {"allow_paths": ["**"]}},
                    }
                ),
            ),
        )
    )
    policy = resolve_review_policy(snapshot, ("src/app.py",))

    assert policy.for_path("src/app.py").auto_approval.allow_path_groups == (
        (),
        ("src/**",),
    )
    decision = evaluate_auto_approval(
        policy,
        _event(),
        _diff("src/app.py", "src/app.py"),
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "path_not_allowlisted"


def test_auto_approval_refuses_any_provider_other_than_github():
    """Diffuse is GitHub-only; the guard is the last line if an event slips through.

    `PullRequestEvent` already rejects a foreign provider at construction, so the
    event has to be forced past that check to reach `evaluate_auto_approval`.
    """
    event = _event()
    object.__setattr__(event, "provider", "gitlab")

    decision = evaluate_auto_approval(
        _policy({"auto_approval": {"enabled": True}}),
        event,
        _diff(),
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "unsupported_provider"


def test_auto_approval_filters_are_all_required_and_renames_check_both_paths():
    diff = _diff("src/legacy.py", "docs/legacy.md")
    policy = _policy(
        {
            "auto_approval": {
                "enabled": True,
                "filters": {
                    "allow_paths": ["**"],
                    "exclude_paths": ["src"],
                    "include_authors": ["octo*"],
                    "include_branches": ["main"],
                    "labels": ["safe-*"],
                    "include_keywords": ["guide"],
                    "include_repositories": ["owner/*"],
                    "file_change_limit": 2,
                },
            }
        },
        ("src/legacy.py", "docs/legacy.md"),
    )

    decision = evaluate_auto_approval(
        policy,
        _event(title="Update the guide"),
        diff,
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "excluded_path"
    assert decision.changed_paths == ("docs/legacy.md", "src/legacy.py")


def test_auto_approval_cascades_and_requires_every_touched_scope():
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {
                            "enabled": True,
                            "risk_ceiling": "high",
                            "filters": {"include_authors": ["octo*"]},
                        },
                    }
                ),
            ),
            PolicyLayer(
                directory_path="src",
                source_path="src/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {
                            "risk_ceiling": "medium",
                            "filters": {"include_authors": ["octocat"]},
                        },
                    }
                ),
            ),
            PolicyLayer(
                directory_path="src/sensitive",
                source_path="src/sensitive/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {"enabled": False},
                    }
                ),
            ),
        )
    )
    policy = resolve_review_policy(
        snapshot,
        ("src/app.py", "src/sensitive/value.py"),
    )
    diff = _diff("src/app.py", "src/app.py") + _diff(
        "src/sensitive/value.py",
        "src/sensitive/value.py",
    )

    decision = evaluate_auto_approval(
        policy,
        _event(changed_file_count=2),
        diff,
        _report(diff_file_count=2, reviewed_file_count=2),
    )

    assert policy.auto_approval_requested
    assert not decision.eligible
    assert decision.reason_code == "disabled"
    assert decision.risk_ceiling is AutoApprovalRisk.MEDIUM


def test_nested_auto_approval_cannot_weaken_parent_constraints():
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {
                            "enabled": True,
                            "risk_ceiling": "high",
                            "filters": {
                                "allow_paths": ["src/**"],
                                "include_authors": ["release-bot"],
                                "exclude_paths": ["src/generated"],
                                "file_change_limit": 10,
                            },
                        },
                    }
                ),
            ),
            PolicyLayer(
                directory_path="src",
                source_path="src/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {
                        "version": 1,
                        "auto_approval": {
                            "enabled": True,
                            "risk_ceiling": "critical",
                            "filters": {
                                "allow_paths": ["**"],
                                "include_authors": ["octocat"],
                                "file_change_limit": 100,
                            },
                        },
                    }
                ),
            ),
        )
    )
    policy = resolve_review_policy(snapshot, ("src/app.py",))
    resolved = policy.for_path("src/app.py")
    assert resolved is not None
    assert resolved.auto_approval.enabled
    assert resolved.auto_approval.risk_ceiling == "high"
    assert resolved.auto_approval.file_change_limit == 10
    assert resolved.auto_approval.exclude_paths == ("src/generated",)
    assert resolved.auto_approval.include_author_groups == (
        ("release-bot",),
        ("octocat",),
    )
    # The nested `**` is rooted at the scope that wrote it, so the two groups agree here
    # rather than the child's catch-all replacing the parent's narrower grant.
    assert resolved.auto_approval.allow_path_groups == (
        ("src/**",),
        ("src/**",),
    )

    decision = evaluate_auto_approval(
        policy,
        _event(author="octocat"),
        _diff("src/app.py", "src/app.py"),
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "author_not_included"


def test_nested_auto_approval_enable_cannot_override_parent_veto():
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {"version": 1, "auto_approval": {"enabled": False}}
                ),
            ),
            PolicyLayer(
                directory_path="docs",
                source_path="docs/.diffuse/config.json",
                config=RepositoryConfig.model_validate(
                    {"version": 1, "auto_approval": {"enabled": True}}
                ),
            ),
        )
    )
    policy = resolve_review_policy(snapshot, ("docs/guide.md",))

    decision = evaluate_auto_approval(
        policy,
        _event(),
        _diff(),
        _report(),
    )

    assert policy.auto_approval_requested is False
    assert not decision.eligible
    assert decision.reason_code == "disabled"


@pytest.mark.parametrize(
    ("paths", "file_count", "line_count", "diff_chars", "expected"),
    [
        (("docs/guide.md",), 1, 100, 8_000, AutoApprovalRisk.LOW),
        (("src/app.py",), 3, 120, 20_000, AutoApprovalRisk.MEDIUM),
        (("package.json",), 1, 4, 1_000, AutoApprovalRisk.HIGH),
        (("src/auth/session.py",), 1, 4, 1_000, AutoApprovalRisk.CRITICAL),
    ],
)
def test_change_risk_is_conservative_and_explainable(
    paths,
    file_count,
    line_count,
    diff_chars,
    expected,
):
    assert (
        assess_change_risk(
            paths,
            changed_file_count=file_count,
            changed_line_count=line_count,
            diff_chars=diff_chars,
        )
        is expected
    )


def test_critical_risk_is_never_approved_even_with_critical_ceiling():
    diff = _diff("src/auth/session.py", "src/auth/session.py")
    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "risk_ceiling": "critical",
                    "filters": {"allow_paths": ["src/**"]},
                }
            },
            ("src/auth/session.py",),
        ),
        _event(),
        diff,
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "critical_risk"


@pytest.mark.parametrize(
    ("report", "unresolved", "reason"),
    [
        (
            _report(ignored_file_count=1, reviewed_file_count=0),
            (),
            "incomplete_review",
        ),
        (
            _report(
                risk_score=2,
                findings=[
                    ReviewFinding(
                        fingerprint="f" * 64,
                        title="Fix the behavior",
                        body="The changed behavior is incorrect.",
                        severity=Severity.LOW,
                        category=Category.CORRECTNESS,
                        confidence=0.9,
                        file_path="docs/guide.md",
                        line=1,
                        side="RIGHT",
                        evidence="The new text contradicts the contract.",
                    )
                ],
            ),
            (),
            "review_not_clean",
        ),
        (
            _report(),
            (
                ReviewFinding(
                    fingerprint="e" * 64,
                    title="Earlier issue remains",
                    body="An earlier issue remains open.",
                    severity=Severity.HIGH,
                    category=Category.CORRECTNESS,
                    confidence=0.9,
                    file_path="docs/guide.md",
                    line=1,
                    side="RIGHT",
                    evidence="The active lineage remains unresolved.",
                ),
            ),
            "review_not_clean",
        ),
        (
            _report(confidence_score=4),
            (),
            "review_not_clean",
        ),
    ],
)
def test_auto_approval_requires_full_clean_review(report, unresolved, reason):
    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "filters": {"allow_paths": ["docs/**"]},
                }
            }
        ),
        _event(),
        _diff(),
        report,
        unresolved_findings=unresolved,
    )

    assert not decision.eligible
    assert decision.reason_code == reason


POLICY_BEARING_PATHS = (
    ".diffuse/config.json",
    ".diffuse/rules.md",
    ".diffuse/files.json",
    "services/api/.diffuse/config.json",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".cursorrules",
    "packages/web/AGENTS.md",
    "packages/web/CONTRIBUTING.md",
    ".cursor/rules/review.mdc",
    "packages/web/.cursor/rules/review.mdc",
    ".github/copilot-instructions.md",
    "packages/web/.github/copilot-instructions.md",
)


@pytest.mark.parametrize("path", POLICY_BEARING_PATHS)
def test_policy_bearing_paths_are_critical_risk(path):
    assert (
        assess_change_risk(
            (path,),
            changed_file_count=1,
            changed_line_count=20,
            diff_chars=800,
        )
        is AutoApprovalRisk.CRITICAL
    )


@pytest.mark.parametrize("path", POLICY_BEARING_PATHS)
def test_policy_bearing_paths_are_never_auto_approved(path):
    """The built-in critical floor outranks an operator allowlist that names the path.

    An operator can be talked into allowlisting `**` -- by a contributor, or by a pull
    request that edits the configuration one commit earlier. The floor is what makes that
    survivable: allowlisting a policy-bearing path still cannot approve a change to it,
    so no pull request can widen the rules that approve it.
    """
    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "risk_ceiling": "critical",
                    "filters": {"allow_paths": ["**", "**/.diffuse/**"]},
                }
            },
            (path,),
        ),
        _event(),
        _diff(path, path),
        _report(),
    )

    assert not decision.eligible
    assert decision.reason_code == "critical_risk"


def test_foreign_ci_definitions_stay_critical_on_a_github_repository():
    """CI definitions stay critical even for a provider Diffuse no longer supports.

    A GitHub repository can still carry a .gitlab-ci.yml, and that file decides what
    runs against the repository, so it must never become auto-approvable.
    """
    assert (
        assess_change_risk(
            (".gitlab-ci.yml",),
            changed_file_count=1,
            changed_line_count=20,
            diff_chars=800,
        )
        is AutoApprovalRisk.CRITICAL
    )


def test_ordinary_documentation_stays_auto_approvable():
    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "filters": {"allow_paths": ["docs/**"]},
                }
            },
            ("docs/contributing.md",),
        ),
        _event(),
        _diff("docs/contributing.md", "docs/contributing.md"),
        _report(),
    )

    assert decision.eligible
    assert decision.risk_level is AutoApprovalRisk.LOW


@pytest.mark.parametrize(
    "path",
    (
        "tests/test_billing.py",
        "test/foo.py",
        "src/__tests__/foo.ts",
        "src/foo_test.go",
        "src/foo.test.ts",
        "packages/web/tests/cart.spec.ts",
        "src/test/java/FooTest.java",
    ),
)
def test_a_one_line_test_change_is_not_low_risk(path):
    """Deleting the assertion that guarded something is a one-line diff.

    Both routes to the low tier have to close for test paths, not just the extension
    list: the small-change shortcut would otherwise hand the low tier straight back to
    the smallest and most dangerous version of this change.
    """
    assert (
        assess_change_risk(
            (path,),
            changed_file_count=1,
            changed_line_count=1,
            diff_chars=200,
        )
        is AutoApprovalRisk.MEDIUM
    )


def test_a_test_only_change_needs_a_raised_ceiling_to_be_approved():
    config = {
        "auto_approval": {
            "enabled": True,
            "filters": {"allow_paths": ["tests/**"]},
        }
    }
    event = _event()
    diff = _diff("tests/test_billing.py", "tests/test_billing.py")

    at_default_ceiling = evaluate_auto_approval(
        _policy(config, ("tests/test_billing.py",)),
        event,
        diff,
        _report(),
    )
    at_raised_ceiling = evaluate_auto_approval(
        _policy(
            {"auto_approval": {**config["auto_approval"], "risk_ceiling": "medium"}},
            ("tests/test_billing.py",),
        ),
        event,
        diff,
        _report(),
    )

    assert not at_default_ceiling.eligible
    assert at_default_ceiling.reason_code == "risk_ceiling"
    assert at_raised_ceiling.eligible


class _StubConnection:
    def close(self) -> None:
        return None


def _snapshot_policy(monkeypatch, config: dict, diff_text: str):
    from service.hosted import worker

    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig.model_validate({"version": 1, **config}),
            ),
        )
    )
    monkeypatch.setattr(worker, "get_conn", lambda: _StubConnection())
    monkeypatch.setattr(
        worker,
        "load_repository_policy",
        lambda conn, snapshot_id: snapshot,
    )
    return worker._load_review_policy(41, diff_text)


def test_the_allowlist_is_read_from_the_snapshot_not_from_the_pull_request_head(
    monkeypatch,
):
    """The allowlist can live in the repository only because the head cannot supply it.

    `_load_review_policy` resolves against the indexed default-branch snapshot, so the
    `.diffuse/config.json` a pull request proposes is diff text and nothing more. Were it
    ever read from the head, every gate in this module would be self-service: a PR would
    allowlist its own paths in the same commit it needs approved.
    """
    head_grant = (
        "diff --git a/.diffuse/config.json b/.diffuse/config.json\n"
        "--- a/.diffuse/config.json\n"
        "+++ b/.diffuse/config.json\n"
        "@@ -1 +1 @@\n"
        '-{"version": 1, "auto_approval": {"enabled": true}}\n'
        '+{"version": 1, "auto_approval": {"enabled": true, "filters": '
        '{"allow_paths": ["src/**"]}}}\n'
    ) + _diff("src/app.py", "src/app.py")

    policy = _snapshot_policy(
        monkeypatch,
        {
            "auto_approval": {
                "enabled": True,
                "filters": {"allow_paths": ["docs/**"]},
            }
        },
        head_grant,
    )
    resolved = policy.for_path("src/app.py")
    decision = evaluate_auto_approval(
        policy,
        _event(changed_file_count=2),
        head_grant,
        _report(diff_file_count=2, reviewed_file_count=2),
    )

    assert resolved is not None
    assert resolved.auto_approval.allow_path_groups == (("docs/**",),)
    assert not decision.eligible
    assert decision.reason_code == "path_not_allowlisted"

    granted_by_snapshot = _snapshot_policy(
        monkeypatch,
        {
            "auto_approval": {
                "enabled": True,
                "filters": {"allow_paths": ["src/**"]},
            }
        },
        _diff("src/app.py", "src/app.py"),
    )

    assert evaluate_auto_approval(
        granted_by_snapshot,
        _event(),
        _diff("src/app.py", "src/app.py"),
        _report(),
    ).eligible
