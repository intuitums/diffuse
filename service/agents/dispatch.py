"""Ed25519-signed worker-to-runner session envelopes."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from service.agents.transport_secret import unwrap_capability, wrap_capability
from service.crypto.sealed_secret import is_sealed
from service.review.workspace import DEFAULT_WORKSPACE_LIMITS, SourceArtifact

PRIVATE_KEY_VARIABLE = "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY"
PUBLIC_KEY_VARIABLE = "DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY"
ENVELOPE_PREFIX = "diffuse-dispatch"
# The native runner receives the whole signed dispatch in memory.  Keep the
# diff within the same review window as the API runtime and make the maximum
# outer envelope explicit, rather than letting one unusually large pull
# request bypass the source-artifact budget through a second field.
MAX_DISPATCH_DIFF_CHARS = 400_000
# 16 MiB source archive -> 22.4 MiB inner base64 -> just under 30 MiB outer
# base64, plus a 400k diff and signed-envelope metadata.
MAX_DISPATCH_ENVELOPE_CHARS = 31 * 1024 * 1024


class DispatchEnvelopeError(ValueError):
    """A native runner dispatch is malformed, expired, or untrusted."""


@dataclass(frozen=True)
class DispatchEnvelope:
    session_id: str
    runtime: str
    capability: str
    capability_id: str
    diff_text: str
    source_artifact: SourceArtifact
    expires_at: datetime


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _private_key() -> Ed25519PrivateKey:
    value = os.environ.get(PRIVATE_KEY_VARIABLE, "")
    try:
        raw = _decode(value)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as error:
        raise DispatchEnvelopeError(
            f"{PRIVATE_KEY_VARIABLE} must be a base64url Ed25519 key"
        ) from error


def _public_key() -> Ed25519PublicKey:
    value = os.environ.get(PUBLIC_KEY_VARIABLE, "")
    try:
        raw = _decode(value)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as error:
        raise DispatchEnvelopeError(
            f"{PUBLIC_KEY_VARIABLE} must be a base64url Ed25519 key"
        ) from error


def validate_dispatch_private_key() -> None:
    _private_key()


def validate_dispatch_public_key() -> None:
    _public_key()


def sign_dispatch(envelope: DispatchEnvelope) -> str:
    if envelope.source_artifact.manifest_digest is None:
        raise DispatchEnvelopeError("dispatch requires a workspace manifest digest")
    if len(envelope.diff_text) > MAX_DISPATCH_DIFF_CHARS:
        raise DispatchEnvelopeError("dispatch diff exceeds max characters")
    if len(envelope.source_artifact.archive) > DEFAULT_WORKSPACE_LIMITS.max_archive_bytes:
        raise DispatchEnvelopeError("dispatch source archive exceeds max bytes")
    # The capability bearer is sealed with the runner-shared transport secret
    # before the Ed25519 signature so a sniffed envelope does not reveal a live
    # Review Access Grant. capability_id stays clear for routing/audit only.
    wrapped_capability = wrap_capability(envelope.capability)
    payload = {
        "session_id": envelope.session_id,
        "runtime": envelope.runtime,
        "capability": wrapped_capability,
        "capability_id": envelope.capability_id,
        "diff_text": envelope.diff_text,
        "source_archive": _encode(envelope.source_artifact.archive),
        "source_digest": envelope.source_artifact.digest,
        "source_manifest_digest": envelope.source_artifact.manifest_digest,
        "exp": int(envelope.expires_at.astimezone(UTC).timestamp()),
    }
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _encode(_private_key().sign(body.encode("ascii")))
    token = f"{ENVELOPE_PREFIX}.{body}.{signature}"
    if len(token) > MAX_DISPATCH_ENVELOPE_CHARS:
        raise DispatchEnvelopeError("dispatch envelope exceeds max characters")
    return token


def verify_dispatch(token: str, *, now: datetime | None = None) -> DispatchEnvelope:
    if len(token) > MAX_DISPATCH_ENVELOPE_CHARS:
        raise DispatchEnvelopeError("dispatch envelope exceeds max characters")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_PREFIX:
        raise DispatchEnvelopeError("dispatch envelope is malformed")
    _, body, signature = parts
    try:
        _public_key().verify(_decode(signature), body.encode("ascii"))
        payload: Any = json.loads(_decode(body))
        encoded_archive = payload["source_archive"]
        if not isinstance(encoded_archive, str) or len(encoded_archive) > (
            4 * ((DEFAULT_WORKSPACE_LIMITS.max_archive_bytes + 2) // 3)
        ):
            raise ValueError
        manifest_digest = payload["source_manifest_digest"]
        if not isinstance(manifest_digest, str):
            raise ValueError
        source_artifact = SourceArtifact(
            _decode(encoded_archive),
            manifest_digest=manifest_digest,
        )
        if payload["source_digest"] != source_artifact.digest:
            raise ValueError
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        sealed_capability = str(payload["capability"])
        if not is_sealed(sealed_capability):
            raise ValueError("dispatch capability must be sealed")
        envelope = DispatchEnvelope(
            session_id=str(payload["session_id"]),
            runtime=str(payload["runtime"]),
            capability=unwrap_capability(sealed_capability),
            capability_id=str(payload["capability_id"]),
            diff_text=str(payload["diff_text"]),
            source_artifact=source_artifact,
            expires_at=expires_at,
        )
    except (InvalidSignature, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DispatchEnvelopeError("dispatch envelope is invalid") from error
    if envelope.expires_at <= (now or datetime.now(UTC)).astimezone(UTC):
        raise DispatchEnvelopeError("dispatch envelope is expired")
    if (
        envelope.runtime not in {"claude", "codex"}
        or not envelope.session_id
        or not envelope.capability
        or not envelope.capability_id
        or len(envelope.diff_text) > MAX_DISPATCH_DIFF_CHARS
        or len(envelope.source_artifact.archive) > DEFAULT_WORKSPACE_LIMITS.max_archive_bytes
    ):
        raise DispatchEnvelopeError("dispatch envelope is invalid")
    return envelope
