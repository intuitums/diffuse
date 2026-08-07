"""Long-lived, credential-isolated agent-runner process.

Gate B intentionally exposes no review-execution route: the runner is brought
up and hardened independently before Gate C lets the worker submit a CLI
review.  Its lifespan runs the same compartment assertions as a real session,
so a healthy process is evidence of the actual container boundary rather than
only a Compose declaration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from service.review.agent_compartment import preflight


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    preflight()
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
