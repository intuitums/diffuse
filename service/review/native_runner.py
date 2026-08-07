"""Worker-side client for the isolated native review runners."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import httpx

from service.agents.dispatch import DispatchEnvelope, sign_dispatch
from service.models.review import ReviewFinding, ReviewReport
from service.review.request import ReviewRequest
from service.storage.agent_session import accept_agent_session_completion


@dataclass(frozen=True)
class NativeSessionDispatch:
    session_id: str
    runtime: str
    capability: str
    capability_id: str
    expires_at: datetime


class NativeRunnerError(RuntimeError):
    """The selected independent runner cannot safely complete a review."""


class NativeRunnerRuntime:
    """Delegate one review to its credential-isolated CLI runner."""

    def __init__(self, runtime: str) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return self._runtime

    def generate(self, request: ReviewRequest) -> ReviewReport:
        if request.agent_session is None:
            raise NativeRunnerError("native runner dispatch requires a durable agent session")
        session = request.agent_session
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
                expires_at=session.expires_at,
            )
        )
        try:
            response = httpx.post(
                f"{url}/v1/reviews",
                json={"envelope": envelope},
                timeout=610,
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
        accept_agent_session_completion(
            conn,
            session_id=session.session_id,
            runtime=session.runtime,
            capability_id=session.capability_id,
            result=result,
        )


def runner_url(runtime: str) -> str:
    return os.environ.get(
        f"DIFFUSE_AGENT_{runtime.upper()}_RUNNER_URL", f"http://agent-runner-{runtime}:8010"
    ).rstrip("/")


def validate_native_runners() -> None:
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
    findings = []
    for item in payload["findings"]:  # type: ignore[index]
        finding = dict(item)  # type: ignore[arg-type]
        material = json.dumps(finding, sort_keys=True, separators=(",", ":")).encode()
        finding["fingerprint"] = hashlib.sha256(material).hexdigest()
        findings.append(ReviewFinding.model_validate(finding))
    changed_paths = {
        line[4:] for line in request.diff_text.splitlines() if line.startswith("+++ b/")
    }
    return ReviewReport(
        summary=str(payload["summary"]),
        risk_score=float(payload["risk_score"]),
        confidence_score=3,
        findings=findings,
        diff_file_count=len(changed_paths),
        reviewed_file_count=len(changed_paths),
        ignored_file_count=0,
        context_chunk_count=0,
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
    )
