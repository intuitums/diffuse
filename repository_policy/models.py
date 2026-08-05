"""Strict schemas and immutable records for repository policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bumping this is mandatory whenever the policy models or policy_fingerprint
# change shape. The version feeds INDEX_FORMAT_VERSION, so existing snapshots
# become format-incompatible and are rebuilt. Without the bump, a stored
# snapshot's persisted policy_fingerprint no longer matches its recomputed value,
# RepositoryPolicySnapshot.__post_init__ raises, and that ValueError is
# classified non-retryable -- so every configured repository's next review fails
# terminally instead of taking the documented reindex path.
POLICY_SCHEMA_VERSION = "repository-policy-v13-auto-approval-allowlist"
REVIEW_PASS_NAMES = ("correctness", "security", "performance", "tests")
ReviewPassName = Literal["correctness", "security", "performance", "tests"]
SeverityName = Literal["critical", "high", "medium", "low"]
AutoApprovalRiskName = Literal["low", "medium", "high", "critical"]
CategoryName = Literal[
    "correctness",
    "security",
    "performance",
    "reliability",
    "testing",
    "architecture",
    "maintainability",
    "api",
]
RULE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_GLOB_LENGTH = 512
MAX_FILTER_PATTERN_LENGTH = 256
CONTEXT_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
PRIVATE_KEY_EXTENSIONS = frozenset(
    {
        ".asc",
        ".der",
        ".gpg",
        ".jks",
        ".kdbx",
        ".key",
        ".keystore",
        ".p12",
        ".p8",
        ".pem",
        ".pfx",
        ".pkcs12",
        ".ppk",
    }
)
SENSITIVE_FILENAMES = frozenset(
    {
        ".git-credentials",
        ".htpasswd",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "_netrc",
        "credentials",
        "credentials.json",
        "secring.gpg",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
        "serviceaccount.json",
    }
)
# Private-key material is conventionally named for its algorithm, with or without a
# trailing qualifier (`id_rsa`, `id_ed25519_deploy`), and carries no extension at all.
SENSITIVE_FILENAME_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
# Credential stores that identify themselves by directory rather than by file name.
SENSITIVE_DIRECTORIES = frozenset({".aws", ".azure", ".gcloud", ".gnupg", ".ssh"})
ENV_TEMPLATE_SUFFIXES = (".dist", ".example", ".sample", ".template", ".tmpl")


def validate_repo_glob(value: str) -> str:
    """Validate one repository-relative, slash-separated glob."""
    value = value.strip()
    if not value or len(value) > MAX_GLOB_LENGTH:
        raise ValueError("path globs must contain between 1 and 512 characters")
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ValueError("path globs must be repository-relative and use forward slashes")
    if any(part == ".." for part in value.split("/")):
        raise ValueError("path globs cannot traverse parent directories")
    return value


def validate_repo_path(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 1024
        or value.startswith(("/", "\\"))
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("paths must be normalized repository-relative paths")
    return value


def is_sensitive_repo_path(value: str) -> bool:
    """Report whether a repository path is secret-shaped.

    Diffuse never ships these files to a model provider, so both the indexer and the
    repository-policy loader must agree on one definition; a repository must not be able
    to opt its own secrets back in by naming them as review context.

    This is a conservative name-based filter for conventionally named credential files,
    not a secret scanner: it cannot recognize a key pasted into `docs/notes.md`.
    """
    posix_path = PurePosixPath(value)
    name = posix_path.name.lower()
    if any(part.lower() in SENSITIVE_DIRECTORIES for part in posix_path.parts[:-1]):
        return True
    # Checked-in placeholders (`.env.example`, `secrets.yaml.template`) carry no secret
    # and are frequently the clearest statement of a repository's configuration surface.
    if name.endswith(ENV_TEMPLATE_SUFFIXES):
        return False
    if name in SENSITIVE_FILENAMES or name.startswith(SENSITIVE_FILENAME_PREFIXES):
        return True
    if PurePosixPath(name).suffix in PRIVATE_KEY_EXTENSIONS:
        return True
    # Both `.env`/`.envrc`/`.env.production` and the equally common `prod.env` shape.
    return name.startswith(".env") or name.endswith(".env")


def validate_filter_pattern(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > MAX_FILTER_PATTERN_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("filter patterns must contain 1 to 256 printable characters")
    if value.count("{") != value.count("}") or value.count("{") > 8:
        raise ValueError("filter-pattern alternation braces must be balanced and bounded")
    return value


def validate_keyword(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("keywords must contain 1 to 200 printable characters")
    return value


class StrictPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutputSectionSettingsPatch(StrictPolicyModel):
    included: bool | None = None
    collapsible: bool | None = None
    default_open: bool | None = None


class ReviewSettingsPatch(StrictPolicyModel):
    enabled: bool | None = None
    passes: tuple[ReviewPassName, ...] | None = Field(default=None, min_length=1, max_length=4)
    minimum_confidence: float | None = Field(default=None, ge=0, le=1)
    minimum_severity: SeverityName | None = None
    ignored_paths: tuple[str, ...] = Field(default=(), max_length=100)
    summary_only: bool | None = None
    respond_to_comments: bool | None = None
    update_description: bool | None = None
    summary_comment: bool | None = None
    fix_with_agent: bool | None = None
    summary_section: OutputSectionSettingsPatch = Field(
        default_factory=OutputSectionSettingsPatch
    )
    issues_table_section: OutputSectionSettingsPatch = Field(
        default_factory=OutputSectionSettingsPatch
    )
    confidence_score_section: OutputSectionSettingsPatch = Field(
        default_factory=OutputSectionSettingsPatch
    )
    diagram: OutputSectionSettingsPatch = Field(
        default_factory=OutputSectionSettingsPatch
    )
    hide_footer: bool | None = None

    @field_validator("passes")
    @classmethod
    def unique_passes(
        cls,
        value: tuple[ReviewPassName, ...] | None,
    ) -> tuple[ReviewPassName, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("review passes must be unique")
        return value

    @field_validator("ignored_paths")
    @classmethod
    def valid_ignored_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class TriggerSettingsPatch(StrictPolicyModel):
    automatic: bool | None = None
    review_drafts: bool | None = None
    review_updates: bool | None = None
    labels: tuple[str, ...] | None = Field(default=None, max_length=100)
    disabled_labels: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_authors: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_authors: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_branches: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_branches: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_keywords: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_keywords: tuple[str, ...] | None = Field(default=None, max_length=100)
    file_change_limit: int | None = Field(default=None, ge=1, le=100_000)
    status_check: bool | None = None
    failure_comment: bool | None = None
    blocking_severities: tuple[SeverityName, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=4,
    )

    @field_validator(
        "labels",
        "disabled_labels",
        "include_authors",
        "exclude_authors",
        "include_branches",
        "exclude_branches",
    )
    @classmethod
    def valid_filter_patterns(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_filter_pattern(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("filter patterns must be unique ignoring case")
        return normalized

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def valid_keywords(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_keyword(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("keywords must be unique ignoring case")
        return normalized

    @field_validator("blocking_severities")
    @classmethod
    def unique_blocking_severities(
        cls,
        value: tuple[SeverityName, ...] | None,
    ) -> tuple[SeverityName, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("blocking severities must be unique")
        return value


class RepositoryRule(StrictPolicyModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    guidance: Annotated[str, Field(min_length=1, max_length=6000)]
    applies_to: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)
    enabled: bool = True
    severity: SeverityName = "medium"
    category: CategoryName = "maintainability"

    @field_validator("applies_to")
    @classmethod
    def valid_applies_to(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class RuleOverride(StrictPolicyModel):
    enabled: bool | None = None
    severity: SeverityName | None = None
    category: CategoryName | None = None


class ContextSettingsPatch(StrictPolicyModel):
    repos: tuple[str, ...] | None = Field(default=None, max_length=7)

    @field_validator("repos")
    @classmethod
    def valid_repositories(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(item.strip() for item in value)
        if any(
            not CONTEXT_REPOSITORY_PATTERN.fullmatch(item)
            or len(item) > 512
            or any(part in {".", ".."} for part in item.split("/"))
            for item in normalized
        ):
            raise ValueError(
                "context repositories must be safe owner/repo names"
            )
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("context repositories must be unique ignoring case")
        return normalized


class SecuritySettingsPatch(StrictPolicyModel):
    preventative: bool | None = None
    preventative_minimum_confidence: float | None = Field(
        default=None,
        ge=0.75,
        le=1,
    )


class AutoApprovalFiltersPatch(StrictPolicyModel):
    # Approving is a write action, so the paths it may touch are named by the operator
    # rather than left to a denylist Diffuse maintains on their behalf. A denylist has
    # to anticipate every sensitive directory in every repository Diffuse is installed
    # on: `**/auth/**` never catches `internal/perms/`, `lib/rbac/`, or `pkg/tenancy/`,
    # and the resulting miss silently approves. Only the operator knows which of those
    # their repository has, so an unnamed path is not consent. Omitting this key leaves
    # the scope approving nothing; an empty list says the same thing explicitly.
    allow_paths: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_paths: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_authors: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_authors: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_branches: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_branches: tuple[str, ...] | None = Field(default=None, max_length=100)
    labels: tuple[str, ...] | None = Field(default=None, max_length=100)
    disabled_labels: tuple[str, ...] | None = Field(default=None, max_length=100)
    include_keywords: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_keywords: tuple[str, ...] | None = Field(default=None, max_length=100)
    file_change_limit: int | None = Field(default=None, ge=1, le=100_000)
    include_repositories: tuple[str, ...] | None = Field(default=None, max_length=100)
    exclude_repositories: tuple[str, ...] | None = Field(default=None, max_length=100)

    @field_validator("allow_paths", "exclude_paths")
    @classmethod
    def valid_path_globs(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_repo_glob(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("auto-approval path globs must be unique")
        return normalized

    @field_validator(
        "include_authors",
        "exclude_authors",
        "include_branches",
        "exclude_branches",
        "labels",
        "disabled_labels",
        "include_repositories",
        "exclude_repositories",
    )
    @classmethod
    def valid_filter_patterns(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_filter_pattern(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("auto-approval filter patterns must be unique ignoring case")
        return normalized

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def valid_keywords(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(validate_keyword(item) for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("auto-approval keywords must be unique ignoring case")
        return normalized


class AutoApprovalSettingsPatch(StrictPolicyModel):
    enabled: bool | None = None
    risk_ceiling: AutoApprovalRiskName | None = None
    filters: AutoApprovalFiltersPatch = Field(
        default_factory=AutoApprovalFiltersPatch
    )


class RepositoryConfig(StrictPolicyModel):
    version: Literal[1]
    review: ReviewSettingsPatch = Field(default_factory=ReviewSettingsPatch)
    triggers: TriggerSettingsPatch = Field(default_factory=TriggerSettingsPatch)
    context: ContextSettingsPatch = Field(default_factory=ContextSettingsPatch)
    security: SecuritySettingsPatch = Field(default_factory=SecuritySettingsPatch)
    auto_approval: AutoApprovalSettingsPatch = Field(
        default_factory=AutoApprovalSettingsPatch
    )
    rules: tuple[RepositoryRule, ...] = Field(default=(), max_length=100)
    rule_overrides: dict[
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")],
        RuleOverride,
    ] = Field(default_factory=dict, max_length=100)

    @field_validator("rules")
    @classmethod
    def unique_rule_ids(
        cls,
        value: tuple[RepositoryRule, ...],
    ) -> tuple[RepositoryRule, ...]:
        ids = [rule.id for rule in value]
        if len(ids) != len(set(ids)):
            raise ValueError("rule IDs must be unique within one config file")
        return value


class ContextFile(StrictPolicyModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    description: Annotated[str | None, Field(max_length=500)] = None
    applies_to: tuple[str, ...] = Field(default=("**",), min_length=1, max_length=32)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return validate_repo_path(value)

    @field_validator("applies_to")
    @classmethod
    def valid_applies_to(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_repo_glob(item) for item in value)


class ContextFilesConfig(StrictPolicyModel):
    version: Literal[1]
    files: tuple[ContextFile, ...] = Field(default=(), max_length=100)


@dataclass(frozen=True)
class PolicyLayer:
    directory_path: str
    source_path: str
    config: RepositoryConfig


@dataclass(frozen=True)
class GuidanceDocument:
    directory_path: str
    source_path: str
    kind: Literal["instructions", "rules", "context"]
    applies_to: tuple[str, ...]
    content: str
    content_hash: str
    description: str | None = None
    priority: int = 0


def policy_fingerprint(
    layers: tuple[PolicyLayer, ...],
    guidance_documents: tuple[GuidanceDocument, ...],
) -> str:
    payload = {
        "schema": POLICY_SCHEMA_VERSION,
        "layers": [
            {
                "directory_path": layer.directory_path,
                "source_path": layer.source_path,
                "config": layer.config.model_dump(mode="json"),
            }
            for layer in layers
        ],
        "guidance_documents": [
            {
                "directory_path": document.directory_path,
                "source_path": document.source_path,
                "kind": document.kind,
                "applies_to": document.applies_to,
                "content_hash": document.content_hash,
                "description": document.description,
                "priority": document.priority,
            }
            for document in guidance_documents
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class RepositoryPolicySnapshot:
    layers: tuple[PolicyLayer, ...] = ()
    guidance_documents: tuple[GuidanceDocument, ...] = ()
    fingerprint: str = ""

    def __post_init__(self) -> None:
        expected = policy_fingerprint(self.layers, self.guidance_documents)
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected)
        elif self.fingerprint != expected:
            raise ValueError("Repository policy fingerprint does not match its contents")


EMPTY_POLICY_FINGERPRINT = policy_fingerprint((), ())
