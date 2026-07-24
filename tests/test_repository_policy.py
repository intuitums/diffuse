import json
import subprocess
from pathlib import Path

import pytest

from repository_policy.discovery import discover_repository_policy
from repository_policy.models import (
    ContextSettingsPatch,
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
)
from repository_policy.resolve import (
    ApprovedCustomContext,
    ApprovedLearnedRule,
    PullRequestTriggerContext,
    apply_approved_custom_contexts,
    apply_approved_learned_rules,
    evaluate_trigger,
    filter_matches,
    path_matches,
    resolve_review_policy,
)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _commit(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "diffuse-tests@example.invalid")
    _git(repo, "config", "user.name", "Diffuse Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "policy fixture")


def test_repository_policy_discovers_tracked_cascading_configuration(tmp_path: Path):
    _write(
        tmp_path,
        ".diffuse/config.json",
        json.dumps(
            {
                "version": 1,
                "review": {
                    "passes": ["correctness", "security"],
                    "minimum_confidence": 0.8,
                    "ignored_paths": ["vendor/**"],
                },
                "rules": [
                    {
                        "id": "auth-boundary",
                        "title": "Protect authorization boundaries",
                        "guidance": "Flag changed code that trusts caller-supplied account IDs.",
                        "applies_to": ["src/**"],
                        "severity": "high",
                        "category": "security",
                    }
                ],
            }
        ),
    )
    _write(
        tmp_path,
        "src/.diffuse/config.json",
        json.dumps(
            {
                "version": 1,
                "review": {
                    "minimum_confidence": 0.9,
                    "summary_only": True,
                    "respond_to_comments": False,
                },
                "rule_overrides": {"auth-boundary": {"severity": "critical"}},
            }
        ),
    )
    _write(tmp_path, ".diffuse/rules.md", "Database writes must remain tenant scoped.\n")
    _write(tmp_path, "AGENTS.md", "Use repository architecture boundaries.\n")
    _write(tmp_path, "docs/security.md", "All account IDs originate outside the trust boundary.\n")
    _write(
        tmp_path,
        ".diffuse/files.json",
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": "docs/security.md",
                        "description": "Security model",
                        "applies_to": ["src/auth/**"],
                    }
                ],
            }
        ),
    )
    _write(tmp_path, "src/auth/login.py", "def login():\n    pass\n")
    _write(tmp_path, "src/public.py", "PUBLIC = True\n")
    _write(tmp_path, "vendor/copied.py", "COPIED = True\n")
    _commit(tmp_path)

    snapshot = discover_repository_policy(tmp_path)
    resolved = resolve_review_policy(
        snapshot,
        {"src/auth/login.py", "src/public.py", "vendor/copied.py"},
        default_passes=("tests",),
        default_minimum_confidence=0.6,
    )

    assert len(snapshot.fingerprint) == 64
    assert [layer.source_path for layer in snapshot.layers] == [
        ".diffuse/config.json",
        "src/.diffuse/config.json",
    ]
    auth = resolved.for_path("src/auth/login.py")
    assert auth is not None
    assert auth.reviewable
    assert auth.passes == ("correctness", "security")
    assert auth.minimum_confidence == 0.9
    assert auth.summary_only
    assert not auth.respond_to_comments
    assert [(rule.id, rule.severity) for rule in auth.rules] == [
        ("auth-boundary", "critical")
    ]
    assert {document.source_path for document in auth.guidance_documents} == {
        ".diffuse/rules.md",
        "AGENTS.md",
        "docs/security.md",
    }

    public = resolved.for_path("src/public.py")
    assert public is not None
    assert public.reviewable
    assert not public.respond_to_comments
    assert {document.source_path for document in public.guidance_documents} == {
        ".diffuse/rules.md",
        "AGENTS.md",
    }
    vendor = resolved.for_path("vendor/copied.py")
    assert vendor is not None
    assert vendor.ignored
    assert not vendor.reviewable
    assert vendor.respond_to_comments
    assert resolved.reviewable_paths == ("src/auth/login.py", "src/public.py")
    assert resolved.passes == ("correctness", "security")
    assert resolved.summary_only
    assert "rule id=auth-boundary" in resolved.prompt_text()
    assert "docs/security.md" in resolved.prompt_text()


def test_greptile_json_imports_safe_public_configuration(tmp_path: Path):
    _write(
        tmp_path,
        "greptile.json",
        json.dumps(
            {
                "strictness": 3,
                "commentTypes": ["logic", "style"],
                "triggerOnUpdates": True,
                "triggerOnDrafts": True,
                "skipReview": "AUTOMATIC",
                "labels": ["review-*"],
                "disabledLabels": ["wip-*"],
                "includeAuthors": ["team-*"],
                "excludeAuthors": ["*-bot"],
                "includeBranches": ["main", "release/*"],
                "excludeBranches": ["draft/**"],
                "includeKeywords": "tenant\nsecurity",
                "ignoreKeywords": "do not review\nprototype",
                "fileChangeLimit": 50,
                "ignorePatterns": "vendor/\n*.generated.py",
                "context": {"repos": ["owner/shared"]},
                "patternRepositories": ["owner/sdk"],
                "instructions": "Preserve tenant boundaries.",
                "customContext": {
                    "rules": [
                        {
                            "rule": "API handlers require authorization.",
                            "scope": ["src/api/**"],
                        }
                    ],
                    "files": [
                        {
                            "path": "docs/style.md",
                            "description": "Service conventions",
                            "scope": ["src/**"],
                        }
                    ],
                    "other": [
                        {
                            "content": "Legacy callers omit optional fields.",
                            "scope": ["legacy/**"],
                        }
                    ],
                },
                "shouldUpdateDescription": True,
                "updateSummaryOnly": True,
                "fixWithAI": False,
                "hideFooter": True,
                "includeIssuesTable": False,
                "includeConfidenceScore": True,
                "includeSequenceDiagram": False,
                "summarySection": {
                    "included": True,
                    "collapsible": True,
                    "defaultOpen": False,
                },
                "issuesTableSection": {"collapsible": True},
                "confidenceScoreSection": {"defaultOpen": False},
                "sequenceDiagramSection": {"defaultOpen": False},
                "statusCheck": True,
                "statusCommentsEnabled": False,
            }
        ),
    )
    _write(tmp_path, "docs/style.md", "Use the shared service boundary.\n")
    _write(tmp_path, "src/api/handler.py", "def handle():\n    pass\n")
    _write(tmp_path, "legacy/adapter.py", "ADAPTER = True\n")
    _write(tmp_path, "vendor/copied.py", "COPIED = True\n")
    _write(tmp_path, "root.generated.py", "GENERATED = True\n")
    _commit(tmp_path)

    snapshot = discover_repository_policy(tmp_path)
    resolved = resolve_review_policy(
        snapshot,
        {
            "src/api/handler.py",
            "legacy/adapter.py",
            "vendor/copied.py",
            "root.generated.py",
        },
    )

    assert [layer.source_path for layer in snapshot.layers] == [
        "greptile.json"
    ]
    api = resolved.for_path("src/api/handler.py")
    assert api is not None
    assert api.minimum_severity == "high"
    assert resolved.allows_severity("src/api/handler.py", "critical")
    assert resolved.allows_severity("src/api/handler.py", "high")
    assert not resolved.allows_severity("src/api/handler.py", "medium")
    assert api.summary_only
    assert api.update_description
    assert not api.summary_comment
    assert not api.fix_with_agent
    assert not api.footer_included
    assert api.summary_section.included
    assert api.summary_section.collapsible
    assert not api.summary_section.default_open
    assert not api.issues_table_section.included
    assert api.issues_table_section.collapsible
    assert api.confidence_score_section.included
    assert not api.confidence_score_section.default_open
    assert not api.diagram_included
    assert not api.diagram_default_open
    assert api.context_repositories == ("owner/shared", "owner/sdk")
    assert [(rule.id, rule.guidance) for rule in api.rules] == [
        ("greptile-rule-001", "API handlers require authorization.")
    ]
    assert {
        document.source_path for document in api.guidance_documents
    } == {
        "greptile.json#instructions",
        "greptile.json#commentTypes",
        "docs/style.md",
    }
    legacy = resolved.for_path("legacy/adapter.py")
    assert legacy is not None
    assert {
        document.source_path for document in legacy.guidance_documents
    } == {
        "greptile.json#instructions",
        "greptile.json#commentTypes",
        "greptile.json#customContext.other[0]",
    }
    assert not resolved.allows_path("vendor/copied.py")
    assert not resolved.allows_path("root.generated.py")
    assert not resolved.triggers.automatic
    assert resolved.triggers.review_drafts
    assert resolved.triggers.review_updates
    assert resolved.triggers.labels == ("review-*",)
    assert resolved.triggers.disabled_labels == ("wip-*",)
    assert resolved.triggers.include_authors == ("team-*",)
    assert resolved.triggers.exclude_authors == ("*-bot",)
    assert resolved.triggers.include_branches == ("main", "release/*")
    assert resolved.triggers.exclude_branches == ("draft/**",)
    assert resolved.triggers.include_keywords == ("security", "tenant")
    assert resolved.triggers.exclude_keywords == (
        "do not review",
        "prototype",
    )
    assert resolved.triggers.file_change_limit == 50
    assert resolved.triggers.status_check
    assert resolved.update_description
    assert not resolved.summary_comment_enabled
    assert not resolved.fix_with_agent_enabled
    assert "Imported greptile.json commentTypes" in resolved.prompt_text()


def test_native_root_diffuse_policy_takes_precedence_over_greptile_json(
    tmp_path: Path,
):
    _write(tmp_path, "greptile.json", '{"unknownCurrentField":true}')
    _write(
        tmp_path,
        ".diffuse/config.json",
        json.dumps(
            {
                "version": 1,
                "review": {
                    "minimum_confidence": 0.91,
                    "minimum_severity": "medium",
                },
            }
        ),
    )
    _write(tmp_path, "app.py", "VALUE = 1\n")
    _commit(tmp_path)

    snapshot = discover_repository_policy(tmp_path)
    resolved = resolve_review_policy(snapshot, {"app.py"})

    assert [layer.source_path for layer in snapshot.layers] == [
        ".diffuse/config.json"
    ]
    assert resolved.for_path("app.py").minimum_confidence == 0.91
    assert resolved.for_path("app.py").minimum_severity == "medium"


def test_nested_native_policy_refines_imported_greptile_root(tmp_path: Path):
    _write(tmp_path, "greptile.json", json.dumps({"strictness": 1}))
    _write(
        tmp_path,
        "src/.diffuse/config.json",
        json.dumps(
            {
                "version": 1,
                "review": {"minimum_severity": "critical"},
            }
        ),
    )
    _write(tmp_path, "app.py", "ROOT = True\n")
    _write(tmp_path, "src/app.py", "NESTED = True\n")
    _commit(tmp_path)

    snapshot = discover_repository_policy(tmp_path)
    resolved = resolve_review_policy(snapshot, {"app.py", "src/app.py"})

    assert [layer.source_path for layer in snapshot.layers] == [
        "greptile.json",
        "src/.diffuse/config.json",
    ]
    assert resolved.for_path("app.py").minimum_severity == "low"
    assert resolved.for_path("src/app.py").minimum_severity == "critical"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"ignorePatterns": "!src/generated.py"}, "negation is not supported"),
        ({"unknownCurrentField": True}, "extra_forbidden"),
    ],
)
def test_greptile_json_rejects_unsupported_or_unknown_migration(
    tmp_path: Path,
    value: dict,
    message: str,
):
    _write(tmp_path, "greptile.json", json.dumps(value))
    _commit(tmp_path)

    with pytest.raises(ValueError, match=message):
        discover_repository_policy(tmp_path)


def test_greptile_json_context_files_must_be_tracked(tmp_path: Path):
    _write(
        tmp_path,
        "greptile.json",
        json.dumps(
            {
                "customContext": {
                    "files": [{"path": "docs/untracked.md"}]
                }
            }
        ),
    )
    _commit(tmp_path)
    _write(tmp_path, "docs/untracked.md", "Not committed.\n")

    with pytest.raises(ValueError, match="untracked or missing"):
        discover_repository_policy(tmp_path)


def test_nested_disable_and_cursor_glob_are_path_scoped(tmp_path: Path):
    _write(
        tmp_path,
        "generated/.diffuse/config.json",
        json.dumps({"version": 1, "review": {"enabled": False}}),
    )
    _write(
        tmp_path,
        ".cursor/rules/python.mdc",
        "---\nglobs: [src/**/*.py, tests/**/*.py]\n---\nUse explicit transaction boundaries.\n",
    )
    _write(tmp_path, "generated/client.py", "CLIENT = True\n")
    _write(tmp_path, "src/app.py", "APP = True\n")
    _write(tmp_path, "README.md", "# Example\n")
    _commit(tmp_path)

    resolved = resolve_review_policy(
        discover_repository_policy(tmp_path),
        {"generated/client.py", "src/app.py", "README.md"},
    )

    assert not resolved.allows_path("generated/client.py")
    assert resolved.allows_path("src/app.py")
    assert resolved.allows_path("README.md")
    python_policy = resolved.for_path("src/app.py")
    readme_policy = resolved.for_path("README.md")
    assert python_policy is not None and len(python_policy.guidance_documents) == 1
    assert readme_policy is not None and readme_policy.guidance_documents == ()


def test_context_repositories_cascade_per_path_and_affect_policy_identity():
    root = PolicyLayer(
        directory_path="",
        source_path=".diffuse/config.json",
        config=RepositoryConfig(
            version=1,
            context=ContextSettingsPatch(repos=("owner/shared",)),
        ),
    )
    nested = PolicyLayer(
        directory_path="src",
        source_path="src/.diffuse/config.json",
        config=RepositoryConfig(
            version=1,
            context=ContextSettingsPatch(repos=("owner/sdk",)),
        ),
    )
    resolved = resolve_review_policy(
        RepositoryPolicySnapshot(layers=(root, nested)),
        ("README.md", "src/app.py"),
    )
    without_context = resolve_review_policy(
        RepositoryPolicySnapshot(),
        ("README.md", "src/app.py"),
    )

    assert resolved.for_path("README.md").context_repositories == ("owner/shared",)
    assert resolved.for_path("src/app.py").context_repositories == ("owner/sdk",)
    assert resolved.context_repositories == ("owner/shared", "owner/sdk")
    assert resolved.fingerprint != without_context.fingerprint


def test_context_repositories_reject_duplicates_and_unsafe_names():
    with pytest.raises(ValueError, match="unique ignoring case"):
        ContextSettingsPatch(repos=("Owner/SDK", "owner/sdk"))
    with pytest.raises(ValueError, match="safe slash-separated"):
        ContextSettingsPatch(repos=("../secrets",))


def test_preventative_security_policy_cascades_and_affects_identity():
    root = PolicyLayer(
        directory_path="",
        source_path=".diffuse/config.json",
        config=RepositoryConfig.model_validate(
            {
                "version": 1,
                "security": {
                    "preventative": True,
                    "preventative_minimum_confidence": 0.94,
                },
            }
        ),
    )
    nested = PolicyLayer(
        directory_path="scripts",
        source_path="scripts/.diffuse/config.json",
        config=RepositoryConfig.model_validate(
            {
                "version": 1,
                "security": {"preventative": False},
            }
        ),
    )
    resolved = resolve_review_policy(
        RepositoryPolicySnapshot(layers=(root, nested)),
        ("src/api.py", "scripts/release.py"),
    )
    default = resolve_review_policy(
        RepositoryPolicySnapshot(),
        ("src/api.py", "scripts/release.py"),
    )

    assert resolved.allows_preventative_security("src/api.py")
    assert resolved.preventative_security_threshold_for("src/api.py") == 0.94
    assert not resolved.allows_preventative_security("scripts/release.py")
    assert resolved.preventative_security_threshold_for("scripts/release.py") == 1
    assert resolved.fingerprint != default.fingerprint

    with pytest.raises(ValueError, match="greater than or equal to 0.75"):
        RepositoryConfig.model_validate(
            {
                "version": 1,
                "security": {
                    "preventative_minimum_confidence": 0.5,
                },
            }
        )


def test_policy_rejects_unknown_overrides_untracked_context_and_duplicate_keys(
    tmp_path: Path,
):
    _write(
        tmp_path,
        ".diffuse/config.json",
        '{"version":1,"version":1}',
    )
    _commit(tmp_path)
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        discover_repository_policy(tmp_path)

    _write(
        tmp_path,
        ".diffuse/config.json",
        json.dumps({"version": 1, "rule_overrides": {"missing-rule": {"enabled": False}}}),
    )
    _git(tmp_path, "add", ".diffuse/config.json")
    _git(tmp_path, "commit", "-m", "unknown override")
    with pytest.raises(ValueError, match="unknown rule IDs"):
        discover_repository_policy(tmp_path)

    _write(tmp_path, ".diffuse/config.json", json.dumps({"version": 1}))
    _write(
        tmp_path,
        ".diffuse/files.json",
        json.dumps({"version": 1, "files": [{"path": "untracked.md"}]}),
    )
    _git(tmp_path, "add", ".diffuse/config.json", ".diffuse/files.json")
    _git(tmp_path, "commit", "-m", "untracked context")
    _write(tmp_path, "untracked.md", "Not committed.\n")
    with pytest.raises(ValueError, match="untracked or missing"):
        discover_repository_policy(tmp_path)


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**", "src/nested/app.py", True),
        ("src/**", "src/nested/app.py", True),
        ("src/*.py", "src/app.py", True),
        ("src/*.py", "src/nested/app.py", False),
        ("**/*.py", "app.py", True),
        ("**/*.py", "src/nested/app.py", True),
        ("docs/", "docs/nested/design.md", True),
        ("src/???.py", "src/app.py", True),
    ],
)
def test_policy_globs_have_deterministic_repository_semantics(
    pattern: str,
    path: str,
    matches: bool,
):
    assert path_matches(pattern, path) is matches


def test_empty_policy_fingerprint_is_stable():
    first = RepositoryPolicySnapshot()
    second = RepositoryPolicySnapshot()

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_approved_learned_rules_are_scoped_fingerprinted_and_prompt_visible():
    base = resolve_review_policy(
        RepositoryPolicySnapshot(),
        {"src/api.py", "tests/test_api.py"},
    )
    learned = ApprovedLearnedRule(
        id=17,
        version=2,
        title="Use the shared request validator",
        guidance="API handlers must validate payloads with the shared schema helper.",
        applies_to=("src/**",),
        severity="high",
        category="api",
    )

    resolved = apply_approved_learned_rules(base, (learned,))

    assert resolved.fingerprint != base.fingerprint
    assert resolved.source_fingerprint == base.source_fingerprint
    assert resolved.approved_learned_rules == (learned,)
    api_policy = resolved.for_path("src/api.py")
    test_policy = resolved.for_path("tests/test_api.py")
    assert api_policy is not None
    assert [(rule.id, rule.source_path) for rule in api_policy.rules] == [
        ("learned-17", "diffuse://learned-rules/17/versions/2")
    ]
    assert test_policy is not None and test_policy.rules == ()
    assert "Use the shared request validator" in resolved.prompt_text()


def test_active_custom_context_is_scoped_fingerprinted_and_prompt_visible():
    base = resolve_review_policy(
        RepositoryPolicySnapshot(),
        {"src/api.py", "tests/test_api.py"},
    )
    context = ApprovedCustomContext(
        id=23,
        context_type="PATTERN",
        body="Account lookups must always include the authenticated tenant.",
        applies_to=("src/**",),
        metadata={"owner": "platform"},
    )

    resolved = apply_approved_custom_contexts(base, (context,))

    assert resolved.fingerprint != base.fingerprint
    assert resolved.approved_custom_contexts == (context,)
    api_policy = resolved.for_path("src/api.py")
    test_policy = resolved.for_path("tests/test_api.py")
    assert api_policy is not None
    assert [
        document.source_path for document in api_policy.guidance_documents
    ] == ["diffuse://custom-context/23"]
    assert test_policy is not None and test_policy.guidance_documents == ()
    assert "authenticated tenant" in resolved.prompt_text()


def _trigger_context(**overrides) -> PullRequestTriggerContext:
    values = {
        "action": "opened",
        "trigger_kind": "automatic",
        "metadata_complete": True,
        "is_draft": False,
        "author": "octocat",
        "base_branch": "main",
        "labels": ("needs-review",),
        "title": "Implement tenant checks",
        "description": "Security-sensitive API update.",
        "changed_file_count": 4,
    }
    values.update(overrides)
    return PullRequestTriggerContext(**values)


def _trigger_policy(config: dict):
    return resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {"version": 1, "triggers": config}
                    ),
                ),
            )
        ),
        {"src/api.py"},
    )


def test_trigger_defaults_skip_drafts_and_updates_but_manual_bypasses_filters():
    policy = resolve_review_policy(RepositoryPolicySnapshot(), {"src/api.py"})

    assert evaluate_trigger(policy, _trigger_context()).eligible
    assert evaluate_trigger(
        policy,
        _trigger_context(is_draft=True),
    ).reason_code == "draft_pull_request"
    assert evaluate_trigger(
        policy,
        _trigger_context(action="synchronize"),
    ).reason_code == "updates_disabled"
    manual = evaluate_trigger(
        policy,
        _trigger_context(
            trigger_kind="manual",
            metadata_complete=False,
            is_draft=True,
        ),
    )
    assert manual.eligible
    assert manual.reason_code == "manual_trigger"


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"labels": ("other",)}, "required_label_missing"),
        ({"labels": ("wip-now",)}, "disabled_label"),
        ({"author": "dependabot[bot]"}, "excluded_author"),
        ({"author": "outside-user"}, "author_not_included"),
        ({"base_branch": "experimental/test"}, "excluded_branch"),
        ({"base_branch": "develop"}, "branch_not_included"),
        ({"title": "Do not review this"}, "excluded_keyword"),
        ({"title": "Routine maintenance", "description": ""}, "required_keyword_missing"),
        ({"changed_file_count": 6}, "file_change_limit"),
    ],
)
def test_trigger_filters_return_stable_skip_reasons(overrides, reason_code):
    policy = _trigger_policy(
        {
            "labels": ["needs-*"],
            "disabled_labels": ["wip-*"],
            "include_authors": ["octo*", "dependabot[bot]"],
            "exclude_authors": ["dependabot[bot]"],
            "include_branches": ["main", "release/{stable,latest}"],
            "exclude_branches": ["experimental/**"],
            "include_keywords": ["tenant"],
            "exclude_keywords": ["do not review"],
            "file_change_limit": 5,
        }
    )

    decision = evaluate_trigger(policy, _trigger_context(**overrides))

    assert not decision.eligible
    assert decision.reason_code == reason_code


def test_cascading_trigger_settings_replace_per_path_and_merge_for_pr():
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "triggers": {
                                "automatic": False,
                                "labels": ["backend"],
                                "file_change_limit": 20,
                                "status_check": False,
                            },
                        }
                    ),
                ),
                PolicyLayer(
                    directory_path="frontend",
                    source_path="frontend/.diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "triggers": {
                                "automatic": True,
                                "review_drafts": True,
                                "labels": [],
                                "file_change_limit": 10,
                                "status_check": True,
                                "blocking_severities": ["medium", "low"],
                            },
                        }
                    ),
                ),
            )
        ),
        {"backend/api.py", "frontend/app.ts"},
    )

    assert policy.triggers.automatic
    assert policy.triggers.review_drafts
    assert policy.triggers.labels == ()
    assert policy.triggers.file_change_limit == 10
    assert policy.triggers.status_check
    assert policy.triggers.blocking_severities == ("medium", "low")
    assert evaluate_trigger(
        policy,
        _trigger_context(is_draft=True, labels=()),
    ).eligible


@pytest.mark.parametrize(
    ("pattern", "value", "matches"),
    [
        ("release/{stable,latest}", "release/stable", True),
        ("release/{stable,latest}", "release/edge", False),
        ("DEPENDABOT[BOT]", "dependabot[bot]", True),
        ("feature/*", "feature/auth", True),
        ("feature/*", "feature/auth/nested", False),
        ("dependabot/**", "dependabot/npm/react", True),
        ("wip-?", "WIP-1", True),
    ],
)
def test_trigger_filter_globs_match_case_insensitively_with_literal_brackets(
    pattern,
    value,
    matches,
):
    assert filter_matches(pattern, value) is matches


def test_diagram_output_settings_cascade_and_any_touched_scope_can_disable():
    policy = resolve_review_policy(
        RepositoryPolicySnapshot(
            layers=(
                PolicyLayer(
                    directory_path="",
                    source_path=".diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "review": {
                                "summary_section": {
                                    "included": True,
                                    "collapsible": True,
                                    "default_open": False,
                                },
                                "diagram": {
                                    "included": True,
                                    "collapsible": False,
                                    "default_open": True,
                                }
                            },
                        }
                    ),
                ),
                PolicyLayer(
                    directory_path="private",
                    source_path="private/.diffuse/config.json",
                    config=RepositoryConfig.model_validate(
                        {
                            "version": 1,
                            "review": {
                                "issues_table_section": {"included": False},
                                "confidence_score_section": {
                                    "included": False
                                },
                                "hide_footer": True,
                                "diagram": {
                                    "included": False,
                                    "default_open": False,
                                }
                            },
                        }
                    ),
                ),
            )
        ),
        {"app.py", "private/secret.py"},
    )

    assert not policy.diagram_included
    assert not policy.diagram_collapsible
    assert not policy.diagram_default_open
    assert policy.summary_section.included
    assert policy.summary_section.collapsible
    assert not policy.summary_section.default_open
    assert not policy.issues_table_section.included
    assert not policy.confidence_score_section.included
    assert not policy.footer_included
