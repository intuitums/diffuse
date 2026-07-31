"""Conservative, explainable eligibility for automatic pull-request approval."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from repository_policy.models import validate_repo_path
from repository_policy.resolve import (
    ResolvedAutoApprovalPolicy,
    ResolvedReviewPolicy,
    filter_matches,
    path_matches,
)
from service.diff_parser import ParsedDiff, parse_unified_diff
from service.models.review import ReviewFinding, ReviewReport
from service.scm import PullRequestEvent

MAX_AUTO_APPROVAL_DIFF_CHARS = 100_000
MAX_AUTO_APPROVAL_CHANGED_LINES = 2_000


class AutoApprovalRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


RISK_ORDER = {
    AutoApprovalRisk.LOW: 0,
    AutoApprovalRisk.MEDIUM: 1,
    AutoApprovalRisk.HIGH: 2,
    AutoApprovalRisk.CRITICAL: 3,
}

CRITICAL_PATH_PATTERNS = (
    # Diffuse's own policy sources decide whether a change may be approved at
    # all, so approving them would let a pull request widen the rules that
    # approved it.
    "**/.diffuse/**",
    # Repository prose steers the reviewer prompt, which makes these files
    # configuration rather than documentation despite their .md extension.
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/CONTRIBUTING.md",
    "**/.cursorrules",
    "**/.cursor/rules/**",
    # Matched at any depth because a nested .github/copilot-instructions.md is
    # discovered as guidance for its own subtree.
    "**/.github/**",
    ".circleci/**",
    ".gitlab-ci.yml",
    "**/.env*",
    "**/api/**",
    "**/auth/**",
    "**/authentication/**",
    "**/authorization/**",
    "**/billing/**",
    "**/payments/**",
    "**/secrets/**",
    "**/migrations/**",
    "**/schema.prisma",
    "**/schema.sql",
    "**/terraform/**",
    "**/*.tf",
    "**/k8s/**",
    "**/kubernetes/**",
    "**/helm/**",
    "**/openapi.*",
    "**/*.proto",
)
HIGH_PATH_PATTERNS = (
    "Dockerfile",
    "**/Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "package.json",
    "**/package.json",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "pyproject.toml",
    "requirements*.txt",
    "**/requirements*.txt",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "**/core/**",
    "**/shared/**",
)
LOW_PATH_PATTERNS = (
    "docs/**",
    "**/docs/**",
    "**/*.md",
    "**/*.mdx",
    "**/*.rst",
    "**/*.txt",
    "tests/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*.css",
    "**/*.scss",
)


@dataclass(frozen=True)
class AutoApprovalDecision:
    eligible: bool
    reason_code: str
    message: str
    risk_level: AutoApprovalRisk
    risk_ceiling: AutoApprovalRisk
    changed_paths: tuple[str, ...]
    changed_file_count: int
    changed_line_count: int
    diff_chars: int


def _matches_path_filter(pattern: str, path: str) -> bool:
    pattern = pattern.rstrip("/")
    if not any(character in pattern for character in "*?{"):
        return path == pattern or path.startswith(f"{pattern}/")
    return path_matches(pattern, path)


def _matches_any_path(patterns: tuple[str, ...], paths: tuple[str, ...]) -> bool:
    return any(
        _matches_path_filter(pattern, path)
        for pattern in patterns
        for path in paths
    )


def _matches_any(patterns: tuple[str, ...], values: tuple[str, ...]) -> bool:
    return any(
        filter_matches(pattern, value)
        for pattern in patterns
        for value in values
    )


def _all_filter_groups_match(
    groups: tuple[tuple[str, ...], ...],
    fallback: tuple[str, ...],
    values: tuple[str, ...],
) -> bool:
    effective_groups = groups or ((fallback,) if fallback else ())
    return all(_matches_any(group, values) for group in effective_groups)


def _all_keyword_groups_match(
    groups: tuple[tuple[str, ...], ...],
    fallback: tuple[str, ...],
    searchable: str,
) -> bool:
    effective_groups = groups or ((fallback,) if fallback else ())
    return all(
        any(keyword.casefold() in searchable for keyword in group)
        for group in effective_groups
    )


def _changed_paths(parsed: ParsedDiff) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                validate_repo_path(path)
                for file in parsed.files
                for path in (file.old_path, file.new_path)
                if path is not None
            }
        )
    )


def _changed_line_count(parsed: ParsedDiff) -> int:
    return sum(
        entry.marker in {"+", "-"}
        for file in parsed.files
        for entry in file.entries
    )


def assess_change_risk(
    changed_paths: tuple[str, ...],
    *,
    changed_file_count: int,
    changed_line_count: int,
    diff_chars: int,
) -> AutoApprovalRisk:
    """Classify inherent change risk without treating a clean review as risk-free."""
    if (
        not changed_paths
        or changed_file_count > 100
        or changed_line_count > MAX_AUTO_APPROVAL_CHANGED_LINES
        or diff_chars > MAX_AUTO_APPROVAL_DIFF_CHARS
        or _matches_any_path(CRITICAL_PATH_PATTERNS, changed_paths)
    ):
        return AutoApprovalRisk.CRITICAL
    if (
        changed_file_count > 20
        or changed_line_count > 400
        or _matches_any_path(HIGH_PATH_PATTERNS, changed_paths)
    ):
        return AutoApprovalRisk.HIGH
    if all(
        _matches_any_path(LOW_PATH_PATTERNS, (path,))
        for path in changed_paths
    ) or (
        changed_file_count <= 2
        and changed_line_count <= 40
        and diff_chars <= 12_000
    ):
        return AutoApprovalRisk.LOW
    return AutoApprovalRisk.MEDIUM


def _strictest_ceiling(
    policies: tuple[ResolvedAutoApprovalPolicy, ...],
) -> AutoApprovalRisk:
    return min(
        (AutoApprovalRisk(item.risk_ceiling) for item in policies),
        key=RISK_ORDER.__getitem__,
        default=AutoApprovalRisk.LOW,
    )


def _decision(
    *,
    eligible: bool,
    reason_code: str,
    message: str,
    risk_level: AutoApprovalRisk,
    risk_ceiling: AutoApprovalRisk,
    changed_paths: tuple[str, ...],
    changed_file_count: int,
    changed_line_count: int,
    diff_chars: int,
) -> AutoApprovalDecision:
    return AutoApprovalDecision(
        eligible=eligible,
        reason_code=reason_code,
        message=message,
        risk_level=risk_level,
        risk_ceiling=risk_ceiling,
        changed_paths=changed_paths,
        changed_file_count=changed_file_count,
        changed_line_count=changed_line_count,
        diff_chars=diff_chars,
    )


def evaluate_auto_approval(
    policy: ResolvedReviewPolicy,
    event: PullRequestEvent,
    diff_text: str,
    report: ReviewReport,
    *,
    unresolved_findings: tuple[ReviewFinding, ...] = (),
) -> AutoApprovalDecision:
    """Require every deterministic policy and clean-review condition to pass."""
    parsed = parse_unified_diff(diff_text)
    try:
        changed_paths = _changed_paths(parsed)
    except ValueError:
        changed_paths = ()
    changed_line_count = _changed_line_count(parsed)
    diff_chars = len(diff_text)
    path_policies = tuple(
        path_policy
        for path in changed_paths
        if (path_policy := policy.for_path(path)) is not None
    )
    approval_policies = tuple(item.auto_approval for item in path_policies)
    risk_ceiling = _strictest_ceiling(approval_policies)
    risk_level = assess_change_risk(
        changed_paths,
        changed_file_count=len(parsed.files),
        changed_line_count=changed_line_count,
        diff_chars=diff_chars,
    )
    details = {
        "risk_level": risk_level,
        "risk_ceiling": risk_ceiling,
        "changed_paths": changed_paths,
        "changed_file_count": len(parsed.files),
        "changed_line_count": changed_line_count,
        "diff_chars": diff_chars,
    }

    def reject(reason_code: str, message: str) -> AutoApprovalDecision:
        return _decision(
            eligible=False,
            reason_code=reason_code,
            message=message,
            **details,
        )

    if event.provider != "github":
        return reject(
            "unsupported_provider",
            "Automatic approval is not implemented for this SCM provider.",
        )
    if not event.metadata_complete:
        return reject(
            "metadata_unavailable",
            "Automatic approval requires authoritative pull-request metadata.",
        )
    if event.is_draft:
        return reject(
            "draft_pull_request",
            "Draft pull requests always require a human to make them ready.",
        )
    if (
        not changed_paths
        or len(parsed.files) != event.changed_file_count
        or len(path_policies) != len(changed_paths)
    ):
        return reject(
            "incomplete_diff",
            "Automatic approval requires a complete, path-resolved pull-request diff.",
        )
    if not approval_policies or any(not item.enabled for item in approval_policies):
        return reject(
            "disabled",
            "Automatic approval is not enabled in every touched path scope.",
        )

    searchable = f"{event.title}\n{event.description}".casefold()
    for approval in approval_policies:
        if _matches_any_path(approval.exclude_paths, changed_paths):
            return reject(
                "excluded_path",
                "A changed path is protected from automatic approval.",
            )
        if _matches_any(approval.exclude_authors, (event.author,)):
            return reject(
                "excluded_author",
                "The pull-request author is excluded from automatic approval.",
            )
        if not _all_filter_groups_match(
            approval.include_author_groups,
            approval.include_authors,
            (event.author,),
        ):
            return reject(
                "author_not_included",
                "The pull-request author is not included for automatic approval.",
            )
        if _matches_any(approval.exclude_branches, (event.base_branch,)):
            return reject(
                "excluded_branch",
                "The target branch is excluded from automatic approval.",
            )
        if not _all_filter_groups_match(
            approval.include_branch_groups,
            approval.include_branches,
            (event.base_branch,),
        ):
            return reject(
                "branch_not_included",
                "The target branch is not included for automatic approval.",
            )
        if _matches_any(approval.disabled_labels, event.labels):
            return reject(
                "disabled_label",
                "A pull-request label excludes automatic approval.",
            )
        if not _all_filter_groups_match(
            approval.label_groups,
            approval.labels,
            event.labels,
        ):
            return reject(
                "required_label_missing",
                "No required automatic-approval label matched.",
            )
        if any(
            keyword.casefold() in searchable
            for keyword in approval.exclude_keywords
        ):
            return reject(
                "excluded_keyword",
                "The pull request contains an excluded automatic-approval keyword.",
            )
        if not _all_keyword_groups_match(
            approval.include_keyword_groups,
            approval.include_keywords,
            searchable,
        ):
            return reject(
                "required_keyword_missing",
                "No required automatic-approval keyword matched.",
            )
        if (
            approval.file_change_limit is not None
            and event.changed_file_count > approval.file_change_limit
        ):
            return reject(
                "file_change_limit",
                "The pull request exceeds an automatic-approval file limit.",
            )
        if _matches_any(
            approval.exclude_repositories,
            (event.repo_full_name,),
        ):
            return reject(
                "excluded_repository",
                "The repository is excluded from automatic approval.",
            )
        if not _all_filter_groups_match(
            approval.include_repository_groups,
            approval.include_repositories,
            (event.repo_full_name,),
        ):
            return reject(
                "repository_not_included",
                "The repository is not included for automatic approval.",
            )

    if risk_level is AutoApprovalRisk.CRITICAL:
        return reject(
            "critical_risk",
            "Critical-risk changes are never automatically approved.",
        )
    if RISK_ORDER[risk_level] > RISK_ORDER[risk_ceiling]:
        return reject(
            "risk_ceiling",
            "The assessed change risk exceeds the configured ceiling.",
        )
    if not report.publication_enabled:
        return reject(
            "review_not_published",
            "Only a published review can authorize automatic approval.",
        )
    if (
        report.diff_file_count != len(parsed.files)
        or report.reviewed_file_count != report.diff_file_count
        or report.ignored_file_count != 0
    ):
        return reject(
            "incomplete_review",
            "Automatic approval requires complete review coverage with no ignored files.",
        )
    if (
        report.findings
        or unresolved_findings
        or report.risk_score != 0
        or report.confidence_score != 5
    ):
        return reject(
            "review_not_clean",
            "Automatic approval requires a clean 5/5-equivalent review.",
        )
    return _decision(
        eligible=True,
        reason_code="approved",
        message=(
            "The review is clean and every automatic-approval policy check passed."
        ),
        **details,
    )
