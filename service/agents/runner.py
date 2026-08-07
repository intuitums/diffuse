"""Long-lived, credential-isolated native CLI review runner."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from service.agents.contract.result import AgentSessionResult
from service.agents.dispatch import (
    DispatchEnvelopeError,
    validate_dispatch_public_key,
    verify_dispatch,
)
from service.agents.errors import AgentSessionAuthRequired
from service.agents.profiles import REVIEW
from service.agents.session import run_structured
from service.review.agent_compartment import preflight
from service.review.agent_host import cli_status, resolve_cli


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    preflight()
    validate_dispatch_public_key()
    yield


app = FastAPI(
    title="Diffuse isolated agent runner",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/v1/status")
def runner_status() -> dict[str, object]:
    """Return only operator-safe readiness state; never vendor identity/output."""

    runtime = os.environ.get("DIFFUSE_AGENT_RUNTIME", "").strip()
    cli = resolve_cli(runtime)
    status = cli_status(cli)
    if status.get("ready"):
        state = "ready"
    elif status.get("installed") and not status.get("authenticated"):
        state = "not_logged_in"
    else:
        state = "auth_required"
    return {
        "runtime": runtime,
        "image_version": os.environ.get("DIFFUSE_RUNNER_IMAGE_VERSION", "unknown"),
        "cli_version": status.get("version"),
        "policy_state": "current" if status.get("sandbox_settings_current") else "not_current",
        "state": state,
    }


class ReviewInvocation(BaseModel):
    envelope: str


@app.post("/v1/reviews")
def review(invocation: ReviewInvocation) -> dict[str, object]:
    """Execute one bounded CLI review in an empty ephemeral workspace."""

    from tempfile import TemporaryDirectory

    try:
        dispatch = verify_dispatch(invocation.envelope)
    except DispatchEnvelopeError as error:
        raise HTTPException(status_code=401, detail="invalid dispatch") from error
    cli = resolve_cli(dispatch.runtime)
    if dispatch.runtime != os.environ.get("DIFFUSE_AGENT_RUNTIME", "").strip():
        raise HTTPException(status_code=403, detail="runner runtime mismatch")
    prompt = (
        "Review the following pull-request diff. Return only the requested JSON schema. "
        "Do not execute shell commands or modify files.\n\n" + dispatch.diff_text
    )
    try:
        with TemporaryDirectory(prefix="diffuse-native-review-") as temporary:
            result, prompt_tokens, completion_tokens = run_structured(
                cli,
                AgentSessionResult,
                system_prompt="You are an independent, precise code reviewer.",
                user_prompt=prompt,
                workspace=Path(temporary),
                tools=None,
                profile=REVIEW,
                capability=dispatch.capability,
                tool_url=os.environ["DIFFUSE_AGENT_TOOL_URL"],
            )
    except AgentSessionAuthRequired as error:
        # The response is intentionally generic: vendor raw errors and device
        # login material must never cross from a credential compartment.
        raise HTTPException(status_code=401, detail="agent_auth_required") from error
    if result.runtime != dispatch.runtime:
        raise ValueError("runner result runtime did not match the selected runtime")
    return {
        **result.model_dump(mode="json"),
        "session_id": dispatch.session_id,
        "capability_id": dispatch.capability_id,
        "runtime": dispatch.runtime,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
