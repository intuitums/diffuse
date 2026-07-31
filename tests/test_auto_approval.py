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
        _policy({"auto_approval": {"enabled": True}}),
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
        _policy({"auto_approval": {"enabled": True}}),
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
    decision = evaluate_auto_approval(
        _policy(
            {
                "auto_approval": {
                    "enabled": True,
                    "risk_ceiling": "critical",
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
        _policy({"auto_approval": {"enabled": True}}, ("docs/contributing.md",)),
        _event(),
        _diff("docs/contributing.md", "docs/contributing.md"),
        _report(),
    )

    assert decision.eligible
    assert decision.risk_level is AutoApprovalRisk.LOW
