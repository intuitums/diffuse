"""Prompt framing shared by the server and isolated Agent Hosts."""

from __future__ import annotations

import re

UNTRUSTED_POLICY_TAG = "untrusted_repository_policy"
PROMPT_STRUCTURAL_TAGS = (
    UNTRUSTED_POLICY_TAG,
    "untrusted_pull_request_diff",
    "untrusted_retrieved_repository_context",
    "untrusted_original_diff_hunk",
    "untrusted_prior_thread_conversation",
    "untrusted_human_question",
    "untrusted_repository_sources_json",
    "untrusted_candidates",
    "untrusted_review_feedback_json",
    "diffuse_finding_json",
    "repository_review_policy_json",
    "diffuse_security_policy_json",
    "changed_paths_json",
)
_STRUCTURAL_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:" + "|".join(PROMPT_STRUCTURAL_TAGS) + r")[^>]*>",
    re.IGNORECASE,
)
NEUTRALIZED_DELIMITER = "[diffuse removed a forged prompt delimiter]"


def neutralize_prompt_delimiters(text: str) -> str:
    """Replace prompt-structural tags found in repository-authored text."""

    return _STRUCTURAL_TAG_PATTERN.sub(NEUTRALIZED_DELIMITER, text)
