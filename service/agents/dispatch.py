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

PRIVATE_KEY_VARIABLE = "DIFFUSE_AGENT_DISPATCH_PRIVATE_KEY"
PUBLIC_KEY_VARIABLE = "DIFFUSE_AGENT_DISPATCH_PUBLIC_KEY"
ENVELOPE_PREFIX = "diffuse-dispatch"


class DispatchEnvelopeError(ValueError):
    """A native runner dispatch is malformed, expired, or untrusted."""


@dataclass(frozen=True)
class DispatchEnvelope:
    session_id: str
    runtime: str
    capability: str
    capability_id: str
    diff_text: str
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
    payload = {
        "session_id": envelope.session_id,
        "runtime": envelope.runtime,
        "capability": envelope.capability,
        "capability_id": envelope.capability_id,
        "diff_text": envelope.diff_text,
        "exp": int(envelope.expires_at.astimezone(UTC).timestamp()),
    }
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _encode(_private_key().sign(body.encode("ascii")))
    return f"{ENVELOPE_PREFIX}.{body}.{signature}"


def verify_dispatch(token: str, *, now: datetime | None = None) -> DispatchEnvelope:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_PREFIX:
        raise DispatchEnvelopeError("dispatch envelope is malformed")
    _, body, signature = parts
    try:
        _public_key().verify(_decode(signature), body.encode("ascii"))
        payload: Any = json.loads(_decode(body))
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        envelope = DispatchEnvelope(
            session_id=str(payload["session_id"]),
            runtime=str(payload["runtime"]),
            capability=str(payload["capability"]),
            capability_id=str(payload["capability_id"]),
            diff_text=str(payload["diff_text"]),
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
    ):
        raise DispatchEnvelopeError("dispatch envelope is invalid")
    return envelope
