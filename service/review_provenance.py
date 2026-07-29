"""Deterministic pull-request provenance detection and reviewer routing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from service.model_config import (
    ModelExecutionPlan,
    ModelExecutor,
    ModelTarget,
    StructuredOutputMode,
)
from service.model_providers import model_family as resolve_model_family

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
TRAILER_PATTERN = re.compile(
    r"^(?P<key>co-authored-by|assisted-by|generated-by|made-with)"
    r"\s*:\s*(?P<value>[^\r\n]{1,512})\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
EMAIL_PATTERN = re.compile(r"<(?P<email>[^<>\s]{1,254})>\s*$")
MODEL_FAMILIES = frozenset({"anthropic", "google", "openai"})
PROVENANCE_CONFIDENCE_DEFAULT = 0.8

# Author names, author emails, and commit-message trailers are all written by
# whoever produced the commit, so a pull-request author can set any of them with
# `git commit --author=` or a hand-written trailer. They are useful evidence but
# must not on their own reach PROVENANCE_CONFIDENCE_DEFAULT, or the author of a
# change could select which model reviews it. Only identities the SCM itself
# asserts — a bot login, or an email on a commit whose signature the provider
# verified — are allowed above this ceiling.
UNVERIFIED_IDENTITY_MAX_STRENGTH = 0.7


@dataclass(frozen=True)
class CommitMetadata:
    """Bounded commit identity fields returned by an SCM API."""

    sha: str
    message: str
    author_name: str = ""
    author_email: str = ""
    author_login: str = ""
    author_type: str = ""
    committer_name: str = ""
    committer_email: str = ""
    committer_login: str = ""
    committer_type: str = ""
    verified: bool = False

    def __post_init__(self) -> None:
        if not COMMIT_SHA_PATTERN.fullmatch(self.sha):
            raise ValueError("Commit metadata requires a full commit digest")
        values = (
            self.message,
            self.author_name,
            self.author_email,
            self.author_login,
            self.author_type,
            self.committer_name,
            self.committer_email,
            self.committer_login,
            self.committer_type,
        )
        if any(not isinstance(value, str) for value in values):
            raise ValueError("Commit metadata identity fields must be strings")
        if len(self.message.encode("utf-8")) > 128_000:
            raise ValueError("Commit message exceeds the provenance limit")
        if any(len(value) > 512 for value in values[1:]):
            raise ValueError("Commit identity field exceeds the provenance limit")


@dataclass(frozen=True)
class PullRequestCommits:
    commits: tuple[CommitMetadata, ...]
    complete: bool

    def __post_init__(self) -> None:
        if len(self.commits) > 250:
            raise ValueError("Pull-request provenance is limited to 250 commits")


@dataclass(frozen=True)
class ProvenanceSignal:
    tool: str
    model_family: str | None
    source: str
    strength: float
    commit_sha: str = ""

    def __post_init__(self) -> None:
        if self.model_family is not None and self.model_family not in MODEL_FAMILIES:
            raise ValueError("Unsupported provenance model family")
        if not 0 <= self.strength <= 1:
            raise ValueError("Provenance signal strength must be between zero and one")


@dataclass(frozen=True)
class PullRequestProvenance:
    classification: str
    model_family: str | None
    tool: str | None
    confidence: float
    commit_count: int
    ai_commit_count: int
    metadata_complete: bool
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.model_family is not None and self.model_family not in MODEL_FAMILIES:
            raise ValueError("Unsupported provenance model family")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Provenance confidence must be between zero and one")
        if not 0 <= self.ai_commit_count <= self.commit_count <= 250:
            raise ValueError("Provenance commit counts are invalid")

    @classmethod
    def not_evaluated(cls) -> PullRequestProvenance:
        return cls(
            classification="not_evaluated",
            model_family=None,
            tool=None,
            confidence=0,
            commit_count=0,
            ai_commit_count=0,
            metadata_complete=False,
            signals=("review_ineligible",),
        )

    @classmethod
    def unavailable(cls) -> PullRequestProvenance:
        return cls(
            classification="unknown",
            model_family=None,
            tool=None,
            confidence=0,
            commit_count=0,
            ai_commit_count=0,
            metadata_complete=False,
            signals=("commit_metadata_unavailable",),
        )

    @property
    def ai_commit_ratio(self) -> float:
        return self.ai_commit_count / self.commit_count if self.commit_count else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "diffuse-review-provenance-v1",
            "classification": self.classification,
            "model_family": self.model_family,
            "tool": self.tool,
            "confidence": round(self.confidence, 4),
            "commit_count": self.commit_count,
            "ai_commit_count": self.ai_commit_count,
            "ai_commit_ratio": round(self.ai_commit_ratio, 4),
            "metadata_complete": self.metadata_complete,
            "signals": list(self.signals),
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ReviewModelPlan:
    candidate_model: str
    verifier_model: str
    reason_code: str
    detected_family: str | None
    executor: str = "litellm"
    structured_output_mode: str = "auto"
    runner_protocol_version: str = "diffuse-model-runner-v1"

    def __post_init__(self) -> None:
        if not self.candidate_model.strip() or not self.verifier_model.strip():
            raise ValueError("Review model plan requires candidate and verifier models")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", self.reason_code):
            raise ValueError("Review model routing reason is invalid")
        ModelExecutor(self.executor)
        StructuredOutputMode(self.structured_output_mode)

    @property
    def execution_plan(self) -> ModelExecutionPlan:
        executor = ModelExecutor(self.executor)
        mode = StructuredOutputMode(self.structured_output_mode)
        return ModelExecutionPlan(
            candidate=ModelTarget(
                executor=executor,
                requested_model=self.candidate_model,
                structured_output_mode=mode,
            ),
            verifier=ModelTarget(
                executor=executor,
                requested_model=self.verifier_model,
                structured_output_mode=mode,
            ),
            runner_protocol_version=self.runner_protocol_version,
        )

    @property
    def fingerprint(self) -> str:
        value = "\0".join(
            (
                self.candidate_model,
                self.verifier_model,
                self.reason_code,
                self.detected_family or "",
                self.execution_plan.fingerprint,
            )
        )
        return hashlib.sha256(value.encode()).hexdigest()


def _identity_signal(
    *,
    name: str,
    email: str,
    login: str,
    actor_type: str,
    source: str,
    commit_sha: str,
    verified: bool,
) -> ProvenanceSignal | None:
    normalized_name = name.strip().casefold()
    normalized_email = email.strip().casefold()
    normalized_login = login.strip().casefold()
    exact_identity = {
        "noreply@anthropic.com": ("claude_code", "anthropic"),
        "cursoragent@cursor.com": ("cursor", None),
        "copilot@github.com": ("github_copilot", None),
        "noreply@openai.com": ("codex", "openai"),
    }
    bot_login = {
        "anthropic-claude[bot]": ("claude_code", "anthropic"),
        "claude[bot]": ("claude_code", "anthropic"),
        "copilot-swe-agent[bot]": ("github_copilot", None),
        "github-copilot[bot]": ("github_copilot", None),
        "openai-codex[bot]": ("codex", "openai"),
        "codex[bot]": ("codex", "openai"),
        "gemini-code-assist[bot]": ("gemini", "google"),
        "devin-ai-integration[bot]": ("devin", None),
    }
    identity_kind = ""
    tool_family = bot_login.get(normalized_login)
    if tool_family is not None:
        identity_kind = "bot_login"
    elif normalized_email in exact_identity:
        tool_family = exact_identity[normalized_email]
        identity_kind = "email"
    if tool_family is None:
        exact_names = {
            "cursor agent": ("cursor", None),
            "github copilot": ("github_copilot", None),
            "openai codex": ("codex", "openai"),
            "gemini code assist": ("gemini", "google"),
        }
        tool_family = exact_names.get(normalized_name)
        if tool_family is not None:
            identity_kind = "name"
    if tool_family is None:
        return None
    is_bot = actor_type.strip().casefold() == "bot" or normalized_login.endswith("[bot]")
    if identity_kind == "bot_login":
        # A bot login comes from the SCM API actor, not from Git, so a
        # pull-request author cannot set it.
        strength = 1.0 if verified and is_bot else 0.98
    elif identity_kind == "email":
        strength = 0.92 if verified else UNVERIFIED_IDENTITY_MAX_STRENGTH
    else:
        strength = UNVERIFIED_IDENTITY_MAX_STRENGTH
    return ProvenanceSignal(
        tool=tool_family[0],
        model_family=tool_family[1],
        source=source,
        strength=strength,
        commit_sha=commit_sha,
    )


def _trailer_signal(
    key: str,
    value: str,
    *,
    commit_sha: str,
) -> ProvenanceSignal | None:
    email_match = EMAIL_PATTERN.search(value)
    email = email_match.group("email") if email_match else ""
    display_name = value[: email_match.start()].strip() if email_match else value.strip()
    source = f"commit_trailer_{key.casefold().replace('-', '_')}"
    identity = _identity_signal(
        name=display_name,
        email=email,
        login="",
        actor_type="",
        source=source,
        commit_sha=commit_sha,
        verified=False,
    )
    if identity is not None:
        # A trailer is part of the commit message, so it carries no signature the
        # provider could have verified. It cannot exceed the unverified ceiling
        # however specific the identity it names looks.
        return ProvenanceSignal(
            tool=identity.tool,
            model_family=identity.model_family,
            source=identity.source,
            strength=UNVERIFIED_IDENTITY_MAX_STRENGTH,
            commit_sha=commit_sha,
        )

    # Match the display name exactly. Scanning the whole trailer value, or
    # accepting a prefix, attributes a human co-author whose given name collides
    # with a product name ("Claude Dubois", "Devin Jones") to that agent.
    normalized = display_name.casefold()
    textual_markers = {
        "claude": ("claude_code", "anthropic"),
        "claude code": ("claude_code", "anthropic"),
        "anthropic": ("claude_code", "anthropic"),
        "cursor": ("cursor", None),
        "cursor agent": ("cursor", None),
        "copilot": ("github_copilot", None),
        "github copilot": ("github_copilot", None),
        "codex": ("codex", "openai"),
        "openai codex": ("codex", "openai"),
        "gemini": ("gemini", "google"),
        "gemini code assist": ("gemini", "google"),
        "devin": ("devin", None),
    }
    marker = textual_markers.get(normalized)
    if marker is not None:
        return ProvenanceSignal(
            tool=marker[0],
            model_family=marker[1],
            source=source,
            strength=UNVERIFIED_IDENTITY_MAX_STRENGTH,
            commit_sha=commit_sha,
        )
    return None


def _commit_identities(
    commit: CommitMetadata,
) -> tuple[tuple[str, str, str, str, str, bool], ...]:
    return (
        (
            commit.author_name,
            commit.author_email,
            commit.author_login,
            commit.author_type,
            "commit_author",
            False,
        ),
        (
            commit.committer_name,
            commit.committer_email,
            commit.committer_login,
            commit.committer_type,
            "commit_committer",
            commit.verified,
        ),
    )


def _commit_signals(commit: CommitMetadata) -> tuple[ProvenanceSignal, ...]:
    signals: list[ProvenanceSignal] = []
    for name, email, login, actor_type, source, verified in _commit_identities(commit):
        signal = _identity_signal(
            name=name,
            email=email,
            login=login,
            actor_type=actor_type,
            source=source,
            commit_sha=commit.sha,
            # Commit-signature verification
            # authenticates the committer identity. A separately configured
            # author remains freely chosen Git metadata.
            verified=verified,
        )
        if signal is not None:
            signals.append(signal)
    for match in TRAILER_PATTERN.finditer(commit.message):
        signal = _trailer_signal(
            match.group("key"),
            match.group("value"),
            commit_sha=commit.sha,
        )
        if signal is not None:
            signals.append(signal)
    return tuple(signals)


def classify_pull_request_provenance(
    commit_set: PullRequestCommits,
    *,
    pull_request_author: str = "",
) -> PullRequestProvenance:
    """Classify bounded SCM metadata without invoking a model."""

    all_signals: list[ProvenanceSignal] = []
    ai_commits: set[str] = set()
    for commit in commit_set.commits:
        signals = _commit_signals(commit)
        if signals:
            ai_commits.add(commit.sha.casefold())
            all_signals.extend(signals)

    pr_signal = _identity_signal(
        name="",
        email="",
        login=pull_request_author,
        actor_type=("bot" if pull_request_author.strip().casefold().endswith("[bot]") else ""),
        source="pull_request_author",
        commit_sha="",
        verified=False,
    )
    if pr_signal is not None:
        all_signals.append(pr_signal)

    if not all_signals:
        return PullRequestProvenance(
            classification=("human_or_undetected" if commit_set.commits else "unknown"),
            model_family=None,
            tool=None,
            confidence=0,
            commit_count=len(commit_set.commits),
            ai_commit_count=0,
            metadata_complete=commit_set.complete,
            signals=(
                ("no_agent_attribution",)
                if commit_set.complete
                else ("incomplete_commit_metadata",)
            ),
        )

    # Once the SCM asserts an identity, forgeable Git fields and trailers remain
    # audit evidence but cannot make that trusted family ambiguous. Otherwise a
    # pull-request author could add a conflicting Made-with/Co-authored-by trailer
    # to keep their own model family in the candidate position. When no asserted
    # identity exists, retain the existing conservative treatment of weak signals.
    asserted_signals = [
        signal for signal in all_signals if signal.strength > UNVERIFIED_IDENTITY_MAX_STRENGTH
    ]
    classification_signals = asserted_signals or all_signals
    families = {signal.model_family for signal in classification_signals if signal.model_family}
    tools = {signal.tool for signal in classification_signals}
    unknown_family_tools = {
        signal.tool for signal in classification_signals if signal.model_family is None
    }
    direct_agent_identity = any(
        signal.source
        in {
            "commit_author",
            "commit_committer",
            "pull_request_author",
        }
        for signal in classification_signals
    )
    ambiguous = len(families) > 1 or bool(families and unknown_family_tools)
    if ambiguous:
        classification = "mixed_ai"
        family = None
        tool = None
    elif families:
        classification = "agent_authored" if direct_agent_identity else "ai_assisted"
        family = next(iter(families))
        tool = next(iter(tools)) if len(tools) == 1 else None
    else:
        classification = (
            "agent_unknown_family" if direct_agent_identity else "ai_assisted_unknown_family"
        )
        family = None
        tool = next(iter(tools)) if len(tools) == 1 else None

    confidence = max(signal.strength for signal in classification_signals)
    if not commit_set.complete:
        confidence *= 0.75
    evidence = sorted({f"{signal.source}:{signal.tool}" for signal in all_signals})
    if not commit_set.complete:
        evidence.append("incomplete_commit_metadata")
    return PullRequestProvenance(
        classification=classification,
        model_family=family,
        tool=tool,
        confidence=round(confidence, 4),
        commit_count=len(commit_set.commits),
        ai_commit_count=len(ai_commits),
        metadata_complete=commit_set.complete,
        signals=tuple(evidence),
    )


def model_family(model: str) -> str | None:
    """Infer a provider family from a LiteLLM/OpenRouter model identifier.

    Re-exported from :mod:`service.model_providers`, which is the single prefix
    table shared with credential and base-URL resolution.
    """

    return resolve_model_family(model)


def select_review_model_plan(
    provenance: PullRequestProvenance,
    *,
    candidate_model: str,
    verifier_model: str,
    minimum_confidence: float = PROVENANCE_CONFIDENCE_DEFAULT,
    executor: str = "litellm",
    structured_output_mode: str = "auto",
) -> ReviewModelPlan:
    """Choose an opposing family when provenance is strong enough."""

    if not 0 <= minimum_confidence <= 1:
        raise ValueError("Provenance confidence threshold must be between zero and one")
    configured = tuple(dict.fromkeys((candidate_model.strip(), verifier_model.strip())))
    if any(not model for model in configured):
        raise ValueError("Configured review models cannot be empty")

    origin = provenance.model_family if provenance.confidence >= minimum_confidence else None
    if origin is not None:
        opposing = tuple(
            model
            for model in configured
            if model_family(model) is not None and model_family(model) != origin
        )
        if opposing:
            # Routing permutes the configured pair; it never contracts it. Using
            # the opposing model for both stages would make the model that
            # proposes findings the same one that verifies them, losing the
            # independent second opinion REVIEW_VERIFIER_MODEL exists to provide
            # on exactly the changes this feature targets.
            selected = opposing[0]
            remaining = tuple(model for model in configured if model != selected)
            return ReviewModelPlan(
                candidate_model=selected,
                verifier_model=remaining[0] if remaining else selected,
                reason_code=f"opposing_{origin}_reviewer",
                detected_family=origin,
                executor=executor,
                structured_output_mode=structured_output_mode,
            )
        return ReviewModelPlan(
            candidate_model=candidate_model,
            verifier_model=verifier_model,
            reason_code="opposing_model_unavailable",
            detected_family=origin,
            executor=executor,
            structured_output_mode=structured_output_mode,
        )

    if provenance.classification in {
        "agent_unknown_family",
        "ai_assisted_unknown_family",
        "mixed_ai",
    }:
        reason = "ambiguous_agent_cross_review"
    elif provenance.classification == "not_evaluated":
        reason = "review_ineligible"
    elif provenance.model_family is not None:
        reason = "low_confidence_cross_review"
    else:
        reason = "default_cross_review"
    return ReviewModelPlan(
        candidate_model=candidate_model,
        verifier_model=verifier_model,
        reason_code=reason,
        detected_family=provenance.model_family,
        executor=executor,
        structured_output_mode=structured_output_mode,
    )
