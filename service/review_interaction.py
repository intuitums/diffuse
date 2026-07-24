"""Provider-neutral parsing rules for human review-thread interaction."""

from __future__ import annotations

import re
from dataclasses import dataclass

CONVERSATION_MENTION_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])@diffuse(?![A-Za-z0-9_-])"
)
MANUAL_REVIEW_TRIGGER_PATTERN = re.compile(r"(?im)^\s*@diffuse(?:\s|$)")
HUMAN_DISCUSSION_PREFIX = "[human discussion only]"
CONVERSATION_ACKNOWLEDGEMENTS = frozenset(
    {
        "ack",
        "acknowledged",
        "addressed",
        "agreed",
        "done",
        "fixed",
        "got it",
        "looks good",
        "makes sense",
        "ok",
        "okay",
        "resolved",
        "sounds good",
        "thank you",
        "thanks",
        "understood",
    }
)


@dataclass(frozen=True)
class ManualReviewRequest:
    repo_full_name: str
    number: int
    trigger_id: str
    requested_by: str
    requested_at: str


def is_human_only_discussion(body: str) -> bool:
    return body.lstrip().casefold().startswith(HUMAN_DISCUSSION_PREFIX)


def is_diffuse_generated(body: str) -> bool:
    return "<!-- diffuse-" in body


def is_manual_review_trigger(body: str) -> bool:
    return MANUAL_REVIEW_TRIGGER_PATTERN.search(body) is not None


def conversation_question(body: str) -> str | None:
    if is_human_only_discussion(body):
        return None
    if not CONVERSATION_MENTION_PATTERN.search(body):
        return None
    question = CONVERSATION_MENTION_PATTERN.sub("", body).strip(" \t\r\n:,-")
    normalized = re.sub(r"[\W_]+", " ", question.casefold()).strip()
    if not normalized or normalized in CONVERSATION_ACKNOWLEDGEMENTS:
        return None
    return question
