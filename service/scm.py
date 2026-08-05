"""Provider-neutral source-control events used by Diffuse workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from repository_policy.models import validate_repo_path

# GitHub repositories are always owner/repo — nested GitLab-style namespaces
# are rejected at the boundary so onboarded names cannot break API path splits.
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TRIGGER_KINDS = {"automatic", "manual"}
REVIEW_COMMENT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# `urlsplit().hostname` already lowercases and strips the brackets from an IPv6
# literal, so these are the exact forms a parsed origin can present.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PLAINTEXT_ORIGIN_VARIABLE = "DIFFUSE_ALLOW_PLAINTEXT_ORIGINS"


def plaintext_origin_allowed(hostname: str | None) -> bool:
    """Decide whether an http:// origin may still receive a Diffuse credential.

    Every origin normalized here goes on to carry an API token, a clone
    credential, or the OAuth client-secret exchange, so plaintext hands those to
    anyone on path. Loopback is exempt because the traffic never leaves the host
    (and the CLI's own callback listener is loopback-only); the opt-out exists
    for lab instances where the operator has accepted that risk knowingly.
    """
    if hostname in LOOPBACK_HOSTS:
        return True
    raw = os.environ.get(PLAINTEXT_ORIGIN_VARIABLE, "").strip()
    if raw not in {"", "0", "1"}:
        raise ValueError(f"{PLAINTEXT_ORIGIN_VARIABLE} must be 0 or 1")
    return raw == "1"


def scm_api_timeout_seconds() -> float:
    """Return the configured timeout applied to every provider API call."""
    timeout = float(os.environ.get("SCM_API_TIMEOUT_SECONDS", "30"))
    if timeout <= 0:
        raise ValueError("SCM_API_TIMEOUT_SECONDS must be positive")
    return timeout


def normalize_base_url(value: str, *, field_name: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL without credentials")
    if parsed.scheme == "http" and not plaintext_origin_allowed(parsed.hostname):
        raise ValueError(
            f"{field_name} must use https; plaintext is accepted only for loopback "
            f"or when {PLAINTEXT_ORIGIN_VARIABLE}=1"
        )
    return normalized


def normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("updated_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("updated_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def normalize_optional_timestamp(value: str, *, field_name: str) -> str:
    if value == "":
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def validate_repository_name(value: str) -> str:
    if (
        not REPOSITORY_PATTERN.fullmatch(value)
        or any(part in {".", ".."} for part in value.split("/"))
        or len(value) > 512
    ):
        raise ValueError("Repository name must be a safe owner/repo path")
    return value


def validate_branch_name(value: str) -> str:
    invalid = (
        not BRANCH_PATTERN.fullmatch(value)
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part in {"", ".", ".."} for part in value.split("/"))
    )
    if invalid:
        raise ValueError("default_branch is not a safe Git branch name")
    return value


@dataclass(frozen=True)
class PullRequestEvent:
    provider: str
    scm_base_url: str
    api_base_url: str
    repo_full_name: str
    number: int
    web_url: str
    action: str
    head_sha: str
    base_sha: str
    updated_at: str
    delivery_id: str
    author: str = ""
    base_branch: str = ""
    head_branch: str = ""
    is_draft: bool = False
    labels: tuple[str, ...] = ()
    title: str = ""
    description: str = ""
    trigger_kind: str = "automatic"
    trigger_id: str = ""
    metadata_complete: bool = False
    changed_file_count: int = 0
    state: str = "open"
    source_created_at: str = ""
    source_closed_at: str = ""
    source_merged_at: str = ""
    additions: int = 0
    deletions: int = 0

    def __post_init__(self) -> None:
        scm_base_url = normalize_base_url(self.scm_base_url, field_name="scm_base_url")
        api_base_url = normalize_base_url(self.api_base_url, field_name="api_base_url")
        valid = (
            self.provider == "github"
            and validate_repository_name(self.repo_full_name)
            and self.number > 0
            and self.web_url.startswith(f"{scm_base_url}/")
            and ACTION_PATTERN.fullmatch(self.action)
            and COMMIT_SHA_PATTERN.fullmatch(self.head_sha)
            and COMMIT_SHA_PATTERN.fullmatch(self.base_sha)
            and 0 < len(self.delivery_id) <= 255
            and self.trigger_kind in TRIGGER_KINDS
            and 0 <= len(self.trigger_id) <= 255
            and isinstance(self.is_draft, bool)
            and isinstance(self.metadata_complete, bool)
            and isinstance(self.changed_file_count, int)
            and not isinstance(self.changed_file_count, bool)
            and 0 <= self.changed_file_count <= 1_000_000
            and self.state in {"open", "closed", "merged"}
            and isinstance(self.additions, int)
            and not isinstance(self.additions, bool)
            and 0 <= self.additions <= 100_000_000
            and isinstance(self.deletions, int)
            and not isinstance(self.deletions, bool)
            and 0 <= self.deletions <= 100_000_000
        )
        if self.trigger_kind == "manual" and not self.trigger_id:
            valid = False
        if self.metadata_complete:
            try:
                validate_branch_name(self.base_branch)
                validate_branch_name(self.head_branch)
            except ValueError:
                valid = False
            valid = (
                valid
                and 0 < len(self.author) <= 255
                and "\x00" not in self.author
                and 0 < len(self.title) <= 1000
                and "\x00" not in self.title
                and len(self.description) <= 100_000
                and "\x00" not in self.description
                and len(self.labels) <= 100
                and all(
                    isinstance(label, str)
                    and 0 < len(label.strip()) <= 255
                    and "\x00" not in label
                    for label in self.labels
                )
            )
        if not valid:
            raise ValueError("Invalid pull-request event")
        normalized_labels = tuple(
            sorted(
                dict.fromkeys(label.strip() for label in self.labels),
                key=str.casefold,
            )
        )
        if len({label.casefold() for label in normalized_labels}) != len(
            normalized_labels
        ):
            raise ValueError("Pull-request labels must be unique ignoring case")
        object.__setattr__(self, "scm_base_url", scm_base_url)
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "head_sha", self.head_sha.lower())
        object.__setattr__(self, "base_sha", self.base_sha.lower())
        object.__setattr__(self, "labels", normalized_labels)
        object.__setattr__(self, "updated_at", normalize_timestamp(self.updated_at))
        source_created_at = normalize_optional_timestamp(
            self.source_created_at,
            field_name="source_created_at",
        )
        source_closed_at = normalize_optional_timestamp(
            self.source_closed_at,
            field_name="source_closed_at",
        )
        source_merged_at = normalize_optional_timestamp(
            self.source_merged_at,
            field_name="source_merged_at",
        )
        object.__setattr__(
            self,
            "source_created_at",
            source_created_at,
        )
        object.__setattr__(self, "source_closed_at", source_closed_at)
        object.__setattr__(self, "source_merged_at", source_merged_at)
        if (
            (self.state == "open" and (source_closed_at or source_merged_at))
            or (self.state == "closed" and source_merged_at)
            or (source_merged_at and self.state != "merged")
        ):
            raise ValueError(
                "Pull-request lifecycle timestamps do not match its state"
            )
        if source_created_at:
            created_at = datetime.fromisoformat(source_created_at)
            if any(
                datetime.fromisoformat(value) < created_at
                for value in (source_closed_at, source_merged_at)
                if value
            ):
                raise ValueError(
                    "Pull-request lifecycle timestamps precede source creation"
                )

    @property
    def scope_key(self) -> str:
        return (
            f"{self.provider}:{self.scm_base_url}:{self.repo_full_name}:pull_request:{self.number}"
        )

    @property
    def idempotency_key(self) -> str:
        return (
            f"{self.scope_key}:{self.base_sha}:{self.head_sha}:"
            f"{self.trigger_fingerprint}"
        )

    @property
    def trigger_fingerprint(self) -> str:
        payload = {
            "action": self.action,
            "author": self.author,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "is_draft": self.is_draft,
            "labels": self.labels,
            "title": self.title,
            "description": self.description,
            "trigger_kind": self.trigger_kind,
            "trigger_id": self.trigger_id,
            "metadata_complete": self.metadata_complete,
            "changed_file_count": self.changed_file_count,
            "state": self.state,
            "source_created_at": self.source_created_at,
            "source_closed_at": self.source_closed_at,
            "source_merged_at": self.source_merged_at,
            "additions": self.additions,
            "deletions": self.deletions,
            # Retired GitLab-shaped fields kept as constants so trigger
            # fingerprints stay stable across the payload-schema cutover.
            "source_project_id": 0,
            "start_sha": self.base_sha,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def lifecycle_at(self) -> str:
        if self.state == "merged" and self.source_merged_at:
            return self.source_merged_at
        if self.state == "closed" and self.source_closed_at:
            return self.source_closed_at
        if self.action in {"open", "opened"} and self.source_created_at:
            return self.source_created_at
        return self.updated_at

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PullRequestEvent:
        required = {
            "provider",
            "scm_base_url",
            "api_base_url",
            "repo_full_name",
            "number",
            "web_url",
            "action",
            "head_sha",
            "base_sha",
            "updated_at",
            "delivery_id",
        }
        metadata_v1 = {
            "author",
            "base_branch",
            "head_branch",
            "is_draft",
            "labels",
            "title",
            "description",
            "trigger_kind",
            "trigger_id",
            "metadata_complete",
            "changed_file_count",
        }
        metadata_v2 = metadata_v1 | {
            "state",
            "source_created_at",
            "additions",
            "deletions",
        }
        metadata_v3 = metadata_v2 | {
            "source_closed_at",
            "source_merged_at",
        }
        # metadata_v4/v5 carried retired GitLab-shaped fields. Still accepted so
        # in-flight queue payloads deserialize across the deploy that drops them.
        metadata_v4 = metadata_v3 | {
            "source_project_id",
        }
        metadata_v5 = metadata_v4 | {
            "start_sha",
        }
        payload_keys = frozenset(payload)
        if payload_keys not in {
            frozenset(required),
            frozenset(required | metadata_v1),
            frozenset(required | metadata_v2),
            frozenset(required | metadata_v3),
            frozenset(required | metadata_v4),
            frozenset(required | metadata_v5),
        }:
            raise ValueError("Workflow payload does not match the pull-request event schema")
        if metadata_v1.issubset(payload):
            if (
                not isinstance(payload["is_draft"], bool)
                or not isinstance(payload["metadata_complete"], bool)
                or not isinstance(payload["labels"], (list, tuple))
                or not isinstance(payload["changed_file_count"], int)
                or isinstance(payload["changed_file_count"], bool)
            ):
                raise ValueError("Workflow pull-request metadata has invalid types")
            labels = tuple(payload["labels"])
        else:
            labels = ()
        return cls(
            provider=str(payload["provider"]),
            scm_base_url=str(payload["scm_base_url"]),
            api_base_url=str(payload["api_base_url"]),
            repo_full_name=str(payload["repo_full_name"]),
            number=int(payload["number"]),
            web_url=str(payload["web_url"]),
            action=str(payload["action"]),
            head_sha=str(payload["head_sha"]),
            base_sha=str(payload["base_sha"]),
            updated_at=str(payload["updated_at"]),
            delivery_id=str(payload["delivery_id"]),
            author=str(payload.get("author", "")),
            base_branch=str(payload.get("base_branch", "")),
            head_branch=str(payload.get("head_branch", "")),
            is_draft=payload.get("is_draft", False),
            labels=labels,
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            trigger_kind=str(payload.get("trigger_kind", "automatic")),
            trigger_id=str(payload.get("trigger_id", "")),
            metadata_complete=payload.get("metadata_complete", False),
            changed_file_count=payload.get("changed_file_count", 0),
            state=str(payload.get("state", "open")),
            source_created_at=str(payload.get("source_created_at", "")),
            source_closed_at=str(payload.get("source_closed_at", "")),
            source_merged_at=str(payload.get("source_merged_at", "")),
            additions=payload.get("additions", 0),
            deletions=payload.get("deletions", 0),
        )


@dataclass(frozen=True)
class ReviewConversationEvent:
    provider: str
    scm_base_url: str
    api_base_url: str
    repo_full_name: str
    number: int
    delivery_id: str
    external_comment_id: str
    root_comment_id: str
    head_sha: str
    base_sha: str
    comment_commit_sha: str
    author: str
    author_association: str
    created_at: str
    question: str
    file_path: str
    line: int
    side: str
    diff_hunk: str

    def __post_init__(self) -> None:
        scm_base_url = normalize_base_url(self.scm_base_url, field_name="scm_base_url")
        api_base_url = normalize_base_url(self.api_base_url, field_name="api_base_url")
        file_path = validate_repo_path(self.file_path)
        valid = (
            self.provider == "github"
            and validate_repository_name(self.repo_full_name)
            and self.number > 0
            and 0 < len(self.delivery_id) <= 255
            and self.external_comment_id.isdigit()
            and self.root_comment_id.isdigit()
            and COMMIT_SHA_PATTERN.fullmatch(self.head_sha)
            and COMMIT_SHA_PATTERN.fullmatch(self.base_sha)
            and COMMIT_SHA_PATTERN.fullmatch(self.comment_commit_sha)
            and 0 < len(self.author) <= 255
            and "\x00" not in self.author
            and self.author_association in REVIEW_COMMENT_ASSOCIATIONS
            and 0 < len(self.question.strip()) <= 12_000
            and "\x00" not in self.question
            and isinstance(self.line, int)
            and not isinstance(self.line, bool)
            and self.line > 0
            and self.side in {"LEFT", "RIGHT"}
            and len(self.diff_hunk) <= 100_000
            and "\x00" not in self.diff_hunk
        )
        if not valid:
            raise ValueError("Invalid review-conversation event")
        object.__setattr__(self, "scm_base_url", scm_base_url)
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "file_path", file_path)
        object.__setattr__(self, "head_sha", self.head_sha.lower())
        object.__setattr__(self, "base_sha", self.base_sha.lower())
        object.__setattr__(
            self,
            "comment_commit_sha",
            self.comment_commit_sha.lower(),
        )
        object.__setattr__(self, "question", self.question.strip())
        object.__setattr__(self, "created_at", normalize_timestamp(self.created_at))

    @property
    def scope_key(self) -> str:
        return (
            f"{self.provider}:{self.scm_base_url}:{self.repo_full_name}:"
            f"pull_request:{self.number}:review_thread:{self.root_comment_id}"
        )

    @property
    def idempotency_key(self) -> str:
        return f"{self.scope_key}:comment:{self.external_comment_id}"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ReviewConversationEvent:
        required_v1 = {
            "provider",
            "scm_base_url",
            "api_base_url",
            "repo_full_name",
            "number",
            "delivery_id",
            "external_comment_id",
            "root_comment_id",
            "head_sha",
            "base_sha",
            "comment_commit_sha",
            "author",
            "author_association",
            "created_at",
            "question",
            "file_path",
            "line",
            "side",
            "diff_hunk",
        }
        required_v2 = required_v1 | {"thread_id"}
        # required_v2 carried a retired GitLab-shaped thread_id. Still accepted so
        # in-flight conversation jobs deserialize across the deploy that drops it.
        if frozenset(payload) not in {
            frozenset(required_v1),
            frozenset(required_v2),
        }:
            raise ValueError(
                "Workflow payload does not match the review-conversation event schema"
            )
        line = payload["line"]
        if not isinstance(line, int) or isinstance(line, bool):
            raise ValueError("Review-conversation line has an invalid type")
        return cls(
            provider=str(payload["provider"]),
            scm_base_url=str(payload["scm_base_url"]),
            api_base_url=str(payload["api_base_url"]),
            repo_full_name=str(payload["repo_full_name"]),
            number=int(payload["number"]),
            delivery_id=str(payload["delivery_id"]),
            external_comment_id=str(payload["external_comment_id"]),
            root_comment_id=str(payload["root_comment_id"]),
            head_sha=str(payload["head_sha"]),
            base_sha=str(payload["base_sha"]),
            comment_commit_sha=str(payload["comment_commit_sha"]),
            author=str(payload["author"]),
            author_association=str(payload["author_association"]),
            created_at=str(payload["created_at"]),
            question=str(payload["question"]),
            file_path=str(payload["file_path"]),
            line=line,
            side=str(payload["side"]),
            diff_hunk=str(payload["diff_hunk"]),
        )


@dataclass(frozen=True)
class ReviewFeedbackCommentEvent:
    provider: str
    scm_base_url: str
    api_base_url: str
    repo_full_name: str
    number: int
    delivery_id: str
    external_comment_id: str
    root_comment_id: str
    author: str
    author_association: str
    created_at: str
    body: str
    file_path: str

    def __post_init__(self) -> None:
        scm_base_url = normalize_base_url(self.scm_base_url, field_name="scm_base_url")
        api_base_url = normalize_base_url(self.api_base_url, field_name="api_base_url")
        file_path = validate_repo_path(self.file_path)
        valid = (
            self.provider == "github"
            and validate_repository_name(self.repo_full_name)
            and self.number > 0
            and 0 < len(self.delivery_id) <= 255
            and self.external_comment_id.isdigit()
            and self.root_comment_id.isdigit()
            and 0 < len(self.author) <= 255
            and "\x00" not in self.author
            and self.author_association in REVIEW_COMMENT_ASSOCIATIONS
            and 0 < len(self.body.strip()) <= 65_536
            and "\x00" not in self.body
        )
        if not valid:
            raise ValueError("Invalid review-feedback comment event")
        object.__setattr__(self, "scm_base_url", scm_base_url)
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "file_path", file_path)
        object.__setattr__(self, "body", self.body.strip())
        object.__setattr__(self, "created_at", normalize_timestamp(self.created_at))

    @property
    def event_key(self) -> str:
        return f"reply:{self.external_comment_id}:created"


@dataclass(frozen=True)
class FeedbackSyncEvent:
    provider: str
    scm_base_url: str
    api_base_url: str
    repo_full_name: str
    number: int
    root_comment_id: str
    generation: int
    base_sha: str
    head_sha: str

    def __post_init__(self) -> None:
        scm_base_url = normalize_base_url(self.scm_base_url, field_name="scm_base_url")
        api_base_url = normalize_base_url(self.api_base_url, field_name="api_base_url")
        valid = (
            self.provider == "github"
            and validate_repository_name(self.repo_full_name)
            and self.number > 0
            and self.root_comment_id.isdigit()
            and isinstance(self.generation, int)
            and not isinstance(self.generation, bool)
            and self.generation > 0
            and COMMIT_SHA_PATTERN.fullmatch(self.base_sha)
            and COMMIT_SHA_PATTERN.fullmatch(self.head_sha)
        )
        if not valid:
            raise ValueError("Invalid review-feedback sync event")
        object.__setattr__(self, "scm_base_url", scm_base_url)
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "base_sha", self.base_sha.lower())
        object.__setattr__(self, "head_sha", self.head_sha.lower())

    @property
    def scope_key(self) -> str:
        return (
            f"{self.provider}:{self.scm_base_url}:{self.repo_full_name}:"
            f"pull_request:{self.number}:feedback_thread:{self.root_comment_id}"
        )

    @property
    def idempotency_key(self) -> str:
        return f"{self.scope_key}:generation:{self.generation}"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FeedbackSyncEvent:
        required = {
            "provider",
            "scm_base_url",
            "api_base_url",
            "repo_full_name",
            "number",
            "root_comment_id",
            "generation",
            "base_sha",
            "head_sha",
        }
        if set(payload) != required:
            raise ValueError(
                "Workflow payload does not match the review-feedback sync schema"
            )
        generation = payload["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("Review-feedback generation has an invalid type")
        return cls(
            provider=str(payload["provider"]),
            scm_base_url=str(payload["scm_base_url"]),
            api_base_url=str(payload["api_base_url"]),
            repo_full_name=str(payload["repo_full_name"]),
            number=int(payload["number"]),
            root_comment_id=str(payload["root_comment_id"]),
            generation=generation,
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
        )


@dataclass(frozen=True)
class PushEvent:
    provider: str
    scm_base_url: str
    api_base_url: str
    repo_full_name: str
    ref_name: str
    default_branch: str
    before_sha: str
    after_sha: str
    pushed_at: str
    delivery_id: str

    def __post_init__(self) -> None:
        scm_base_url = normalize_base_url(self.scm_base_url, field_name="scm_base_url")
        api_base_url = normalize_base_url(self.api_base_url, field_name="api_base_url")
        validate_repository_name(self.repo_full_name)
        validate_branch_name(self.default_branch)
        valid = (
            self.provider == "github"
            and self.ref_name == f"refs/heads/{self.default_branch}"
            and COMMIT_SHA_PATTERN.fullmatch(self.before_sha)
            and COMMIT_SHA_PATTERN.fullmatch(self.after_sha)
            and 0 < len(self.delivery_id) <= 255
        )
        if not valid:
            raise ValueError("Invalid repository push event")
        object.__setattr__(self, "scm_base_url", scm_base_url)
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "before_sha", self.before_sha.lower())
        object.__setattr__(self, "after_sha", self.after_sha.lower())
        object.__setattr__(self, "pushed_at", normalize_timestamp(self.pushed_at))

    @property
    def scope_key(self) -> str:
        return (
            f"{self.provider}:{self.scm_base_url}:"
            f"{self.repo_full_name}:repository_index:{self.ref_name}"
        )

    @property
    def idempotency_key(self) -> str:
        return f"{self.scope_key}:{self.after_sha}"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PushEvent:
        required = {
            "provider",
            "scm_base_url",
            "api_base_url",
            "repo_full_name",
            "ref_name",
            "default_branch",
            "before_sha",
            "after_sha",
            "pushed_at",
            "delivery_id",
        }
        if set(payload) != required:
            raise ValueError("Workflow payload does not match the push event schema")
        return cls(
            provider=str(payload["provider"]),
            scm_base_url=str(payload["scm_base_url"]),
            api_base_url=str(payload["api_base_url"]),
            repo_full_name=str(payload["repo_full_name"]),
            ref_name=str(payload["ref_name"]),
            default_branch=str(payload["default_branch"]),
            before_sha=str(payload["before_sha"]),
            after_sha=str(payload["after_sha"]),
            pushed_at=str(payload["pushed_at"]),
            delivery_id=str(payload["delivery_id"]),
        )
