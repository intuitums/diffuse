"""Worker-side client for the isolated native review runners."""

from __future__ import annotations

import os
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import httpx

from service.agents.contract.agent import AgentInvestigationRole, opposite_agent_runtime
from service.agents.contract.result import (
    ResultValidationError,
    accept_bound_agent_investigation_result,
    accept_bound_agent_verification_result,
    canonical_result_payload_bytes,
    canonical_result_payload_digest,
)
from service.agents.dispatch import DispatchEnvelope, sign_dispatch
from service.diff_parser import parse_unified_diff
from service.models.review import CandidateFinding, ReviewReport, VerificationDecision
from service.review.report_assembly import (
    all_files_disabled_report,
    deduplicate_candidates_with_ids,
    review_confidence_score,
    review_presentation,
    reviewable_diff,
    risk_floor,
    verified_findings,
)
from service.review.request import ReviewRequest
from service.review.workspace import SourceArtifact
from service.storage.agent_investigation import (
    accept_agent_investigation_completion,
    record_agent_investigation_lifecycle,
    request_agent_investigation_cancellation,
)


@dataclass(frozen=True)
class NativeSessionDispatch:
    """The durable execution spec a worker minted for one candidate or verifier."""

    session_id: str
    runtime: str
    repository_id: int
    pull_request_id: int
    snapshot_id: int
    base_sha: str
    head_sha: str
    capability: str
    capability_id: str
    context_plan_fingerprint: str
    role: AgentInvestigationRole
    turn_budget: int
    timeout_seconds: int
    max_result_bytes: int
    source_artifact: SourceArtifact
    expires_at: datetime
    input_result: dict[str, object] | None = None
    input_result_digest: str | None = None


class NativeRunnerError(RuntimeError):
    """The selected independent runner cannot safely complete a review."""

    def __init__(self, message: str, *, code: str = "runner_execution_failed") -> None:
        super().__init__(message)
        self.code = code


# The CLI gets its dispatched subprocess timeout. Artifact validation,
# temporary workspace setup, and result transport occur outside that budget.
NATIVE_RUNNER_REQUEST_TIMEOUT_GRACE_SECONDS = 120
NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS = 5
NATIVE_RUNNER_POLL_SECONDS = 2
INVESTIGATION_CAPABILITY_HEADER = "X-Diffuse-Investigation-Capability"


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
        candidate_session = request.agent_investigation
        if candidate_session.runtime != self._runtime:
            raise ValueError("native candidate runtime does not match review selection")
        if candidate_session.role is not AgentInvestigationRole.CANDIDATE:
            raise ValueError("native review requires a candidate investigation first")
        if (
            candidate_session.input_result is not None
            or candidate_session.input_result_digest is not None
        ):
            raise ValueError("native candidate investigation cannot carry verifier input")
        candidate_payload = _run_investigation(candidate_session, request)
        candidate_digest = canonical_result_payload_digest(candidate_payload)
        if request.native_verifier_factory is None:
            raise NativeRunnerError("native runner review requires a verifier dispatch")
        verifier_runtime = opposite_agent_runtime(candidate_session.runtime)
        verifier_session = request.native_verifier_factory(
            verifier_runtime,
            candidate_payload,
            candidate_digest,
        )
        if verifier_session.runtime != verifier_runtime:
            raise ValueError("native verifier runtime is not independent from the candidate")
        if verifier_session.role is not AgentInvestigationRole.VERIFIER:
            raise ValueError("native review requires a verifier investigation second")
        if verifier_session.input_result_digest != candidate_digest:
            raise ValueError("native verifier is not bound to the accepted candidate result")
        verifier_payload = _run_investigation(verifier_session, request)
        return _report_from_runner(candidate_payload, verifier_payload, request)


def _run_investigation(
    session: NativeSessionDispatch,
    request: ReviewRequest,
) -> dict[str, object]:
    """Run one candidate or verifier investigation to a validated result."""

    url = runner_url(session.runtime)
    envelope = sign_dispatch(
        DispatchEnvelope(
            session_id=session.session_id,
            runtime=session.runtime,
            repository_id=session.repository_id,
            pull_request_id=session.pull_request_id,
            snapshot_id=session.snapshot_id,
            base_sha=session.base_sha,
            head_sha=session.head_sha,
            capability=session.capability,
            capability_id=session.capability_id,
            context_plan_fingerprint=session.context_plan_fingerprint,
            role=session.role,
            turn_budget=session.turn_budget,
            timeout_seconds=session.timeout_seconds,
            max_result_bytes=session.max_result_bytes,
            diff_text=request.diff_text,
            source_artifact=session.source_artifact,
            input_result=session.input_result,
            input_result_digest=session.input_result_digest,
            expires_at=session.expires_at,
        )
    )
    payload = _start_or_reconnect(url, envelope, session)
    try:
        _record_lifecycle(payload, session)
        deadline = min(
            session.expires_at.timestamp(),
            time.time() + session.timeout_seconds + NATIVE_RUNNER_REQUEST_TIMEOUT_GRACE_SECONDS,
        )
        while payload.get("status") in {"accepted", "running", "cancel_requested"}:
            if time.time() >= deadline:
                _cancel(url, session)
                raise NativeRunnerError(
                    f"{session.runtime} runner exceeded its investigation deadline",
                    code="timeout",
                )
            time.sleep(NATIVE_RUNNER_POLL_SECONDS)
            if request.progress_callback is not None:
                request.progress_callback()
            payload = _investigation_status(url, session)
            _record_lifecycle(payload, session)
        _raise_for_terminal_payload(payload, session)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise NativeRunnerError(f"{session.runtime} runner did not complete the investigation")
        _accept_completion(result, session)
        return result
    except (TypeError, KeyError) as error:
        raise NativeRunnerError(f"{session.runtime} runner returned an invalid result") from error


def _start_or_reconnect(
    url: str, envelope: str, session: NativeSessionDispatch
) -> dict[str, object]:
    """Start once, then reconnect by immutable investigation id on an uncertain POST."""

    try:
        response = httpx.post(
            f"{url}/v1/investigations",
            json={"envelope": envelope},
            timeout=NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
        )
        if response.status_code == 401:
            raise ValueError(f"agent_auth_required:{session.runtime}")
        if response.status_code not in {200, 202}:
            raise NativeRunnerError(
                f"{session.runtime} runner rejected the review",
                code="runner_execution_failed",
            )
        return _lifecycle_payload(response)
    except httpx.HTTPError:
        # The host might have accepted the signed envelope before the network
        # response disappeared. Querying this exact UUID is safe; creating a
        # second investigation is not.
        try:
            return _investigation_status(url, session)
        except NativeRunnerError as error:
            raise NativeRunnerError(
                f"{session.runtime} runner dispatch outcome is unknown",
                code=error.code,
            ) from error


def _investigation_status(url: str, session: NativeSessionDispatch) -> dict[str, object]:
    try:
        response = httpx.get(
            f"{url}/v1/investigations/{session.session_id}",
            headers={INVESTIGATION_CAPABILITY_HEADER: session.capability},
            timeout=NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise NativeRunnerError("runner lifecycle status is unavailable") from error
    if response.status_code == 401:
        raise ValueError(f"agent_auth_required:{session.runtime}")
    if response.status_code != 200:
        raise NativeRunnerError("runner lifecycle status is unavailable")
    return _lifecycle_payload(response)


def _lifecycle_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise NativeRunnerError(
            "runner lifecycle response is invalid",
            code="invalid_result",
        ) from error
    if not isinstance(payload, dict):
        raise NativeRunnerError(
            "runner lifecycle response is invalid",
            code="invalid_result",
        )
    return payload


def _raise_for_terminal_payload(
    payload: dict[str, object],
    session: NativeSessionDispatch,
) -> None:
    status = payload.get("status")
    if status == "completed":
        return
    error_code = str(payload.get("error_code") or "")
    if status == "failed" and error_code == "auth_required":
        raise ValueError(f"agent_auth_required:{session.runtime}")
    if status == "failed" and error_code == "configuration_error":
        raise ValueError(f"agent_configuration_error:{session.runtime}")
    if status == "failed":
        code = error_code or "runner_execution_failed"
        raise NativeRunnerError(_terminal_error_message(session.runtime, code), code=code)
    if status == "cancelled":
        code = error_code or "cancelled"
        raise NativeRunnerError(_terminal_error_message(session.runtime, code), code=code)
    raise NativeRunnerError(f"{session.runtime} runner did not complete the investigation")


def _terminal_error_message(runtime: str, code: str) -> str:
    messages = {
        "cancelled": f"{runtime} runner cancelled the investigation",
        "configuration_error": f"{runtime} runner configuration is invalid",
        "invalid_result": f"{runtime} runner returned an invalid result",
        "invalid_workspace": f"{runtime} runner rejected the dispatched workspace",
        "rate_limited": f"{runtime} runner was rate limited",
        "runner_execution_failed": f"{runtime} runner failed during execution",
        "timeout": f"{runtime} runner exceeded its investigation deadline",
    }
    return messages.get(code, f"{runtime} runner failed with {code}")


def _cancel(url: str, session: NativeSessionDispatch) -> None:
    try:
        response = httpx.post(
            f"{url}/v1/investigations/{session.session_id}/cancel",
            headers={INVESTIGATION_CAPABILITY_HEADER: session.capability},
            timeout=NATIVE_RUNNER_STATUS_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return
        from indexer.store import get_conn

        with closing(get_conn()) as conn, conn:
            request_agent_investigation_cancellation(
                conn,
                session_id=session.session_id,
                runtime=session.runtime,
                capability_id=session.capability_id,
            )
    except httpx.HTTPError:
        # The worker's durable lease expiry and the signed capability limit the
        # remaining impact when a disconnected host cannot receive cancellation.
        return


def _record_lifecycle(payload: dict[str, object], session: NativeSessionDispatch) -> None:
    if (
        payload.get("session_id") != session.session_id
        or payload.get("capability_id") != session.capability_id
        or payload.get("runtime") != session.runtime
    ):
        raise NativeRunnerError("runner lifecycle does not match dispatched session")
    status = payload.get("status")
    if status not in {"accepted", "running"}:
        return
    runner_id = payload.get("runner_id")
    if not isinstance(runner_id, str):
        raise NativeRunnerError("runner lifecycle does not identify its host")
    from indexer.store import get_conn

    with closing(get_conn()) as conn, conn:
        record_agent_investigation_lifecycle(
            conn,
            session_id=session.session_id,
            runtime=session.runtime,
            capability_id=session.capability_id,
            runner_id=runner_id,
            status=status,
        )


def _accept_completion(payload: dict[str, object], session: NativeSessionDispatch) -> None:
    if (
        payload.get("session_id") != session.session_id
        or payload.get("capability_id") != session.capability_id
        or payload.get("runtime") != session.runtime
    ):
        raise NativeRunnerError("runner completion does not match dispatched session")
    try:
        if session.role is AgentInvestigationRole.CANDIDATE:
            accept_bound_agent_investigation_result(
                payload,
                session_id=session.session_id,
                runtime=session.runtime,
                max_result_bytes=session.max_result_bytes,
            )
        else:
            if session.input_result is None or session.input_result_digest is None:
                raise NativeRunnerError("verifier dispatch is missing its candidate input")
            allowed_candidate_ids = {
                f"candidate-{index}"
                for index, _finding in enumerate(session.input_result.get("findings", []))
            }
            accept_bound_agent_verification_result(
                payload,
                session_id=session.session_id,
                runtime=session.runtime,
                candidate_result_digest=session.input_result_digest,
                allowed_candidate_ids=allowed_candidate_ids,
                max_result_bytes=session.max_result_bytes,
            )
    except ResultValidationError as error:
        raise NativeRunnerError(
            f"{session.runtime} runner result failed validation ({error.code})",
            code="invalid_result",
        ) from error
    result = canonical_result_payload_bytes(payload)
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


def validate_agent_clients(runtimes: tuple[str, ...] | None = None) -> None:
    """Fail startup only when the candidate or verifier runner is unavailable."""

    if runtimes is None:
        from service.review.agents import hosted_review_agent_name

        candidate_runtime = hosted_review_agent_name()
        runtimes = tuple(
            dict.fromkeys((candidate_runtime, opposite_agent_runtime(candidate_runtime)))
        )
    for runtime in runtimes:
        try:
            response = httpx.get(f"{runner_url(runtime)}/v1/status", timeout=5)
            state = response.json().get("state") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as error:
            raise ValueError(f"{runtime} runner is unavailable") from error
        if state != "ready":
            raise ValueError(f"{runtime} runner requires operator login or policy repair")


def _report_from_runner(
    candidate_payload: dict[str, object],
    verifier_payload: dict[str, object],
    request: ReviewRequest,
) -> ReviewReport:
    complete_diff = parse_unified_diff(request.diff_text)
    reviewable, ignored_file_count = reviewable_diff(complete_diff, request.policy)
    if complete_diff.files and not reviewable.files:
        return all_files_disabled_report(
            diff_file_count=len(complete_diff.files),
            ignored_file_count=ignored_file_count,
            policy=request.policy,
        )

    raw_candidates = [
        CandidateFinding.model_validate(item)
        for item in candidate_payload["findings"]  # type: ignore[index]
    ]
    candidates, raw_to_filtered_ids = deduplicate_candidates_with_ids(
        raw_candidates,
        reviewable,
        request.policy,
    )
    decisions: dict[str, VerificationDecision] = {}
    for item in verifier_payload["decisions"]:  # type: ignore[index]
        decision = VerificationDecision.model_validate(item)
        filtered_id = raw_to_filtered_ids.get(decision.candidate_id)
        if filtered_id is None:
            continue
        decisions[filtered_id] = decision.model_copy(update={"candidate_id": filtered_id})
    findings = verified_findings(candidates, decisions, set(), request.policy)
    risk_score = (
        min(10, max(float(verifier_payload["risk_score"]), risk_floor(findings)))
        if findings
        else 0
    )
    verifier_prompt_tokens = int(verifier_payload.get("prompt_tokens", 0))
    verifier_completion_tokens = int(verifier_payload.get("completion_tokens", 0))
    prompt_tokens = int(candidate_payload.get("prompt_tokens", 0)) + verifier_prompt_tokens
    completion_tokens = (
        int(candidate_payload.get("completion_tokens", 0)) + verifier_completion_tokens
    )
    return ReviewReport(
        summary=str(verifier_payload["summary"]),
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
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        verifier_prompt_tokens=verifier_prompt_tokens,
        verifier_completion_tokens=verifier_completion_tokens,
        **review_presentation(request.policy),
    )
