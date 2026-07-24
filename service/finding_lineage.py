"""Deterministic finding continuity across pull-request review runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from service.review_models import ReviewFinding

LineageStatus = Literal["active", "addressed"]
TransitionKind = Literal["new", "persistent", "reopened", "addressed"]
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class HistoricalFinding:
    lineage_id: int
    status: LineageStatus
    finding: ReviewFinding


@dataclass(frozen=True)
class FindingTransition:
    kind: TransitionKind
    lineage_id: int | None
    finding: ReviewFinding | None


@dataclass(frozen=True)
class FindingSnapshot:
    lineage_id: int
    title: str
    severity: str
    category: str
    file_path: str
    line: int
    side: str


@dataclass(frozen=True)
class ReviewContinuity:
    new_fingerprints: tuple[str, ...] = ()
    persistent_fingerprints: tuple[str, ...] = ()
    reopened_fingerprints: tuple[str, ...] = ()
    addressed: tuple[FindingSnapshot, ...] = ()
    open_findings: tuple[ReviewFinding, ...] = ()

    @property
    def inline_fingerprints(self) -> frozenset[str]:
        return frozenset(self.new_fingerprints)


def _normalized(value: str) -> str:
    return " ".join(TOKEN_PATTERN.findall(value.casefold()))


def _token_similarity(left: str, right: str) -> float:
    left_tokens = set(TOKEN_PATTERN.findall(left.casefold()))
    right_tokens = set(TOKEN_PATTERN.findall(right.casefold()))
    if not left_tokens or not right_tokens:
        return 0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _finding_similarity(
    current: ReviewFinding,
    historical: ReviewFinding,
) -> float:
    if (
        current.file_path != historical.file_path
        or current.category != historical.category
        or current.security_classification != historical.security_classification
    ):
        return 0
    if current.fingerprint == historical.fingerprint:
        return 1.0

    title = SequenceMatcher(
        None,
        _normalized(current.title),
        _normalized(historical.title),
        autojunk=False,
    ).ratio()
    details = SequenceMatcher(
        None,
        _normalized(f"{current.body} {current.evidence}"),
        _normalized(f"{historical.body} {historical.evidence}"),
        autojunk=False,
    ).ratio()
    tokens = _token_similarity(
        f"{current.title} {current.body}",
        f"{historical.title} {historical.body}",
    )
    line_bonus = max(0.0, 0.08 - min(abs(current.line - historical.line), 80) / 1000)
    return min(0.999, (0.55 * title) + (0.25 * details) + (0.20 * tokens) + line_bonus)


def classify_finding_lineage(
    current_findings: tuple[ReviewFinding, ...],
    historical_findings: tuple[HistoricalFinding, ...],
    *,
    touched_paths: frozenset[str],
    match_threshold: float = 0.68,
) -> tuple[FindingTransition, ...]:
    """Match current findings, then address unmatched active findings on touched files."""
    if not 0 <= match_threshold <= 1:
        raise ValueError("Finding-lineage match threshold must be between zero and one")

    candidates: list[tuple[float, int, int]] = []
    for current_index, current in enumerate(current_findings):
        for history_index, historical in enumerate(historical_findings):
            score = _finding_similarity(current, historical.finding)
            if score >= match_threshold:
                candidates.append((score, current_index, history_index))
    candidates.sort(
        key=lambda item: (
            -item[0],
            historical_findings[item[2]].lineage_id,
            current_findings[item[1]].fingerprint,
        )
    )

    current_matches: dict[int, int] = {}
    matched_history: set[int] = set()
    for _score, current_index, history_index in candidates:
        if current_index in current_matches or history_index in matched_history:
            continue
        current_matches[current_index] = history_index
        matched_history.add(history_index)

    transitions: list[FindingTransition] = []
    for current_index, finding in enumerate(current_findings):
        history_index = current_matches.get(current_index)
        if history_index is None:
            transitions.append(
                FindingTransition(kind="new", lineage_id=None, finding=finding)
            )
            continue
        historical = historical_findings[history_index]
        transitions.append(
            FindingTransition(
                kind="reopened" if historical.status == "addressed" else "persistent",
                lineage_id=historical.lineage_id,
                finding=finding,
            )
        )

    for history_index, historical in enumerate(historical_findings):
        if (
            history_index not in matched_history
            and historical.status == "active"
            and historical.finding.file_path in touched_paths
        ):
            transitions.append(
                FindingTransition(
                    kind="addressed",
                    lineage_id=historical.lineage_id,
                    finding=None,
                )
            )
    return tuple(transitions)
