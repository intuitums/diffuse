"""Worker-side client for the isolated native review runners."""

from __future__ import annotations

import json
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import httpx

from service.agents.dispatch import DispatchEnvelope, sign_dispatch
from service.agents.profiles import REVIEW
from service.diff_parser import parse_unified_diff
from service.models.review import CandidateFinding, ReviewReport, VerificationDecision
from service.review.report_assembly import (
    all_files_disabled_report,
    deduplicate_candidates,
    review_confidence_score,
    review_presentation,
    reviewable_diff,
    risk_floor,
    verified_findings,
)
from service.review.request import ReviewRequest
from service.review.workspace import SourceArtifact
from service.storage.agent_investigation import accept_agent_investigation_completion


@dataclass(frozen=True)
class NativeSessionDispatch:
    session_id: str
    runtime: str
    capability: str
    capability_id: str
    source_artifact: SourceArtifact
    expires_at: datetime


class NativeRunnerError(RuntimeError):
    """The selected independent runner cannot safely complete a review."""


# The CLI gets its full REVIEW timeout.  Artifact validation, temporary
# workspace setup, and result transport occur outside that subprocess budget.
NATIVE_RUNNER_REQUEST_TIMEOUT_SECONDS = REVIEW.timeout_seconds + 120


class AgentRuntime:
    """Delegate one review to its credential-isolated CLI runner."""

    def __init__(self, runtime: str) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime

    def generate(self, request: ReviewRequest) -> ReviewReport:
        if request.agent_investigation is None:
            raise NativeRunnerError("native runner dispatch requires a durable agent session")
        session = request.agent_investigation
        if session.runtime != self._runtime:
            raise NativeRunnerError("native runner session runtime does not match selection")
        url = runner_url(self._runtime)
        envelope = sign_dispatch(
            DispatchEnvelope(
                session_id=session.session_id,
                runtime=session.runtime,
                capability=session.capability,
                capability_id=session.capability_id,
                diff_text=request.diff_text,
                source_artifact=session.source_artifact,
                expires_at=session.expires_at,
            )
        )
        try:
            response = httpx.post(
                f"{url}/v1/reviews",
                json={"envelope": envelope},
                timeout=NATIVE_RUNNER_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            raise NativeRunnerError(f"{self._runtime} runner is unavailable") from error
        if response.status_code == 401:
            raise ValueError(f"agent_auth_required:{self._runtime}")
        if response.status_code != 200:
            raise NativeRunnerError(f"{self._runtime} runner rejected the review")
        try:
            payload = response.json()
            _accept_completion(payload, session)
            return _report_from_runner(payload, request)
        except (TypeError, ValueError, KeyError) as error:
            raise NativeRunnerError(f"{self._runtime} runner returned an invalid result") from error


def _accept_completion(payload: dict[str, object], session: NativeSessionDispatch) -> None:
    if (
        payload.get("session_id") != session.session_id
        or payload.get("capability_id") != session.capability_id
        or payload.get("runtime") != session.runtime
    ):
        raise NativeRunnerError("runner completion does not match dispatched session")
    result = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    from indexer.store import get_conn

    with closing(get_conn()) as conn, conn:
        accept_agent_investigation_completion(
            conn,
            session_id=session.session_id,
            runtime=session.runtime,
            capability_id=session.capability_id,
            result=result,
        )


def runner_url(runtime: str) -> str:
    return os.environ.get(
        f"DIFFUSE_REVIEW_AGENT_{runtime.upper()}_RUNNER_URL", f"http://agent-host-{runtime}:8010"
    ).rstrip("/")


def validate_agent_clients() -> None:
    """Fail startup when either required independent runner is unavailable."""

    for runtime in ("claude", "codex"):
        try:
            response = httpx.get(f"{runner_url(runtime)}/v1/status", timeout=5)
            state = response.json().get("state") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as error:
            raise ValueError(f"{runtime} runner is unavailable") from error
        if state != "ready":
            raise ValueError(f"{runtime} runner requires operator login or policy repair")


def _report_from_runner(payload: dict[str, object], request: ReviewRequest) -> ReviewReport:
    complete_diff = parse_unified_diff(request.diff_text)
    reviewable, ignored_file_count = reviewable_diff(complete_diff, request.policy)
    if complete_diff.files and not reviewable.files:
        return all_files_disabled_report(
            diff_file_count=len(complete_diff.files),
            ignored_file_count=ignored_file_count,
            policy=request.policy,
        )

    # The runner is an investigator, not a publisher.  Its self-reported
    # findings must cross exactly the same immutable boundaries as candidates
    # from the transitional runtime: policy scope, changed-line anchoring,
    # confidence/severity floors, deduplication, publication cap, and stable
    # continuity fingerprint.  Creating a synthetic "keep" decision is
    # intentional: a native session supplies one confidence value rather than
    # the API runtime's candidate/verifier pair, and `verified_findings` owns
    # every other publication invariant.
    candidates = [
        CandidateFinding.model_validate(item)
        for item in payload["findings"]  # type: ignore[index]
    ]
    candidates = deduplicate_candidates(candidates, reviewable, request.policy)
    decisions = {
        f"candidate-{index}": VerificationDecision(
            candidate_id=f"candidate-{index}",
            keep=True,
            confidence=candidate.confidence,
            rationale="Native agent session self-reported this finding.",
        )
        for index, candidate in enumerate(candidates)
    }
    findings = verified_findings(candidates, decisions, set(), request.policy)
    risk_score = (
        min(10, max(float(payload["risk_score"]), risk_floor(findings))) if findings else 0
    )
    summary = (
        str(payload["summary"])
        if findings
        else "No high-confidence actionable issues were found."
    )
    return ReviewReport(
        summary=summary,
        risk_score=risk_score,
        confidence_score=review_confidence_score(
            risk_score=risk_score,
            finding_count=len(findings),
            diff_file_count=len(complete_diff.files),
            reviewed_file_count=len(reviewable.files),
            ignored_file_count=ignored_file_count,
        ),
        findings=findings,
        diff_file_count=len(complete_diff.files),
        reviewed_file_count=len(reviewable.files),
        ignored_file_count=ignored_file_count,
        inline_comments_enabled=(
            not request.policy.summary_only if request.policy is not None else True
        ),
        context_chunk_count=0,
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
        **review_presentation(request.policy),
    )
