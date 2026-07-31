"""Strict records for inspectable review feedback and memory signals."""

from __future__ import annotations

from dataclasses import dataclass

from service.scm import normalize_timestamp


@dataclass(frozen=True)
class ReviewReaction:
    external_id: str
    actor_login: str
    content: str
    created_at: str

    def __post_init__(self) -> None:
        if (
            not self.external_id.isdigit()
            or not 0 < len(self.actor_login) <= 255
            or "\x00" in self.actor_login
            or self.content not in {"+1", "-1"}
        ):
            raise ValueError("Invalid review reaction")
        object.__setattr__(self, "created_at", normalize_timestamp(self.created_at))


@dataclass(frozen=True)
class ReactionSyncResult:
    observed: int
    withdrawn: int
    active_positive: int
    active_negative: int


@dataclass(frozen=True)
class ReviewFeedbackSummary:
    finding_thread_id: int
    root_comment_id: str
    category: str
    severity: str
    file_path: str
    positive_reactions: int
    negative_reactions: int
    context_replies: int
    addressed_outcomes: int
    reopened_outcomes: int
    suppression_protected: bool
    security_classification: str | None = None
