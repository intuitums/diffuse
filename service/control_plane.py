"""Signed, allowlisted metadata projection from a Diffuse data plane."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import time
import urllib.request
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from service.scm import normalize_base_url


class Projection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeploymentProjection(Projection):
    deploymentKey: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["self_hosted", "managed"]
    region: str | None = Field(default=None, max_length=100)
    status: Literal["healthy", "degraded", "offline"]
    version: str = Field(min_length=1, max_length=100)
    updatedAt: int = Field(ge=0)


class RepositoryProjection(Projection):
    externalId: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=512)
    provider: Literal["github", "gitlab"]
    defaultBranch: str = Field(min_length=1, max_length=255)
    indexStatus: Literal["ready", "indexing", "stale", "failed"]
    reviewStatus: Literal["idle", "reviewing", "attention"]
    lastIndexedAt: int | None = Field(default=None, ge=0)
    lastReviewAt: int | None = Field(default=None, ge=0)
    openFindingCount: int = Field(ge=0)
    criticalFindingCount: int = Field(ge=0)
    updatedAt: int = Field(ge=0)


class ReviewProjection(Projection):
    externalId: str = Field(min_length=1, max_length=200)
    repositoryExternalId: str = Field(min_length=1, max_length=200)
    repositoryName: str = Field(min_length=1, max_length=512)
    number: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=500)
    status: Literal["queued", "reviewing", "published", "failed"]
    findingCount: int = Field(ge=0)
    criticalCount: int = Field(ge=0)
    latencyMs: int | None = Field(default=None, ge=0)
    model: str = Field(min_length=1, max_length=512)
    updatedAt: int = Field(ge=0)


class QualityProjection(Projection):
    evaluatedAt: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    truePositives: int = Field(ge=0)
    falsePositives: int = Field(ge=0)
    falseNegatives: int = Field(ge=0)
    addressedFindings: int = Field(ge=0)
    sampleSize: int = Field(ge=0)
    medianLatencyMs: int = Field(ge=0)
    estimatedCostUsd: float = Field(ge=0)


class ModelProjection(Projection):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=512)
    status: Literal["ready", "missing_key", "error"]
    reviewPasses: list[str] = Field(max_length=10)
    updatedAt: int = Field(ge=0)


class ControlPlaneSnapshot(Projection):
    deployment: DeploymentProjection
    repositories: list[RepositoryProjection] = Field(max_length=50)
    reviews: list[ReviewProjection] = Field(max_length=40)
    quality: QualityProjection | None = None
    model: ModelProjection | None = None


def _signing_secret(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("control-plane signing secret must be a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("control-plane signing secret must not be group/world accessible")
    secret = path.read_bytes().strip()
    if len(secret) < 32:
        raise ValueError("control-plane signing secret must contain at least 32 bytes")
    return secret


def publish_snapshot(
    snapshot: ControlPlaneSnapshot,
    *,
    url: str | None = None,
    secret_file: Path | None = None,
) -> None:
    base_url = normalize_base_url(
        url or os.environ.get("DIFFUSE_CONTROL_PLANE_URL", ""),
        field_name="DIFFUSE_CONTROL_PLANE_URL",
    )
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("DIFFUSE_CONTROL_PLANE_URL must use HTTPS off loopback")
    path = secret_file or Path(
        os.environ.get(
            "DIFFUSE_CONTROL_PLANE_SIGNING_SECRET_FILE",
            "/run/secrets/diffuse-control-plane-signing-key",
        )
    )
    secret = _signing_secret(path)
    body = snapshot.model_dump_json(exclude_none=True).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret,
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        f"{base_url}/v1/data-plane/snapshot",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Diffuse-Timestamp": timestamp,
            "X-Diffuse-Signature": f"sha256={signature}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 202:
            raise RuntimeError(f"control plane rejected snapshot with HTTP {response.status}")


def load_snapshot(path: Path) -> ControlPlaneSnapshot:
    if path.is_symlink() or not path.is_file():
        raise ValueError("control-plane snapshot must be a regular file")
    return ControlPlaneSnapshot.model_validate_json(path.read_text())
