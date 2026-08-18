"""Ed25519-signed immutable execution specs for isolated agent investigations."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import TypeAdapter

from diffuse_protocol.artifact import DEFAULT_WORKSPACE_LIMITS, SourceArtifact
from diffuse_protocol.investigation import AgentInvestigationRole, parse_agent_investigation_role
from diffuse_protocol.result import accept_candidate_result_input
from diffuse_protocol.secret import is_sealed
from diffuse_protocol.transport import unwrap_capability, wrap_capability

PRIVATE_KEY_VARIABLE = "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY"
PUBLIC_KEY_VARIABLE = "DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY"
ENVELOPE_PREFIX = "diffuse-dispatch"
# The Agent Host receives the whole signed dispatch in memory. Bound the diff
# separately and cap the outer envelope so one unusually large pull request
# cannot bypass the source-artifact budget through a second field.
MAX_DISPATCH_DIFF_CHARS = 400_000
# 16 MiB source archive -> 22.4 MiB inner base64 -> just under 30 MiB outer
# base64, plus a 400k diff, candidate verification input, and signed metadata.
MAX_DISPATCH_ENVELOPE_CHARS = 32 * 1024 * 1024
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INPUT_RESULT_ADAPTER = TypeAdapter(dict[str, object])


class DispatchEnvelopeError(ValueError):
    """A native runner dispatch is malformed, expired, or untrusted."""


@dataclass(frozen=True)
class DispatchEnvelope:
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
    diff_text: str
    source_artifact: SourceArtifact
    expires_at: datetime
    input_result: dict[str, object] | None = None
    input_result_digest: str | None = None


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


def _require_sha1(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _SHA1_PATTERN.fullmatch(normalized):
        raise DispatchEnvelopeError(f"dispatch {field} must be a 40-character hexadecimal sha")
    return normalized


def _require_sha256(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise DispatchEnvelopeError(f"dispatch {field} must be a 64-character hexadecimal digest")
    return normalized


def _validated_input_result(
    payload: dict[str, object] | None,
    *,
    digest: str | None,
    max_result_bytes: int,
) -> tuple[dict[str, object], str]:
    if payload is None:
        raise DispatchEnvelopeError("dispatch verifier input_result is required")
    if digest is None:
        raise DispatchEnvelopeError("dispatch verifier input_result_digest is required")
    _require_sha256(digest, field="input_result_digest")
    try:
        _INPUT_RESULT_ADAPTER.validate_python(payload)
        _, actual_digest = accept_candidate_result_input(
            payload,
            expected_digest=digest,
            max_result_bytes=max_result_bytes,
        )
    except (TypeError, ValueError) as error:
        raise DispatchEnvelopeError("dispatch verifier input_result is invalid") from error
    return payload, actual_digest


def _validate_envelope(envelope: DispatchEnvelope) -> DispatchEnvelope:
    if envelope.repository_id <= 0:
        raise DispatchEnvelopeError("dispatch repository_id must be positive")
    if envelope.pull_request_id <= 0:
        raise DispatchEnvelopeError("dispatch pull_request_id must be positive")
    if envelope.snapshot_id <= 0:
        raise DispatchEnvelopeError("dispatch snapshot_id must be positive")
    if not envelope.session_id:
        raise DispatchEnvelopeError("dispatch session_id is required")
    if not envelope.capability:
        raise DispatchEnvelopeError("dispatch capability is required")
    if not envelope.capability_id:
        raise DispatchEnvelopeError("dispatch capability_id is required")
    if envelope.runtime not in {"claude", "codex"}:
        raise DispatchEnvelopeError("dispatch runtime is invalid")
    if len(envelope.diff_text) > MAX_DISPATCH_DIFF_CHARS:
        raise DispatchEnvelopeError("dispatch diff exceeds max characters")
    if len(envelope.source_artifact.archive) > DEFAULT_WORKSPACE_LIMITS.max_archive_bytes:
        raise DispatchEnvelopeError("dispatch source archive exceeds max bytes")
    if envelope.source_artifact.manifest_digest is None:
        raise DispatchEnvelopeError("dispatch requires a workspace manifest digest")
    if envelope.turn_budget <= 0:
        raise DispatchEnvelopeError("dispatch turn_budget must be positive")
    if envelope.timeout_seconds <= 0:
        raise DispatchEnvelopeError("dispatch timeout_seconds must be positive")
    if envelope.max_result_bytes <= 0:
        raise DispatchEnvelopeError("dispatch max_result_bytes must be positive")
    _require_sha1(envelope.base_sha, field="base_sha")
    _require_sha1(envelope.head_sha, field="head_sha")
    _require_sha256(envelope.context_plan_fingerprint, field="context_plan_fingerprint")
    _require_sha256(envelope.source_artifact.digest, field="source archive digest")
    _require_sha256(
        envelope.source_artifact.manifest_digest,
        field="source manifest digest",
    )
    role = parse_agent_investigation_role(envelope.role)
    if role is AgentInvestigationRole.CANDIDATE:
        if envelope.input_result is not None or envelope.input_result_digest is not None:
            raise DispatchEnvelopeError("dispatch candidate input_result is forbidden")
    else:
        payload, digest = _validated_input_result(
            envelope.input_result,
            digest=envelope.input_result_digest,
            max_result_bytes=envelope.max_result_bytes,
        )
        object.__setattr__(envelope, "input_result", payload)
        object.__setattr__(envelope, "input_result_digest", digest)
    return envelope


def validate_dispatch_private_key() -> None:
    _private_key()


def validate_dispatch_public_key() -> None:
    _public_key()


def sign_dispatch(envelope: DispatchEnvelope) -> str:
    envelope = _validate_envelope(envelope)
    role = parse_agent_investigation_role(envelope.role)
    # The capability bearer is sealed with the runner-shared transport secret
    # before the Ed25519 signature so a sniffed envelope does not reveal a live
    # Review Access Grant. capability_id stays clear for routing/audit only.
    wrapped_capability = wrap_capability(envelope.capability)
    payload = {
        "session_id": envelope.session_id,
        "runtime": envelope.runtime,
        "repository_id": envelope.repository_id,
        "pull_request_id": envelope.pull_request_id,
        "snapshot_id": envelope.snapshot_id,
        "base_sha": envelope.base_sha,
        "head_sha": envelope.head_sha,
        "capability": wrapped_capability,
        "capability_id": envelope.capability_id,
        "context_plan_fingerprint": envelope.context_plan_fingerprint,
        "role": role.value,
        "turn_budget": envelope.turn_budget,
        "timeout_seconds": envelope.timeout_seconds,
        "max_result_bytes": envelope.max_result_bytes,
        "diff_text": envelope.diff_text,
        "source_archive": _encode(envelope.source_artifact.archive),
        "source_archive_digest": envelope.source_artifact.digest,
        "source_manifest_digest": envelope.source_artifact.manifest_digest,
        "input_result": envelope.input_result,
        "input_result_digest": envelope.input_result_digest,
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
        if payload["source_archive_digest"] != source_artifact.digest:
            raise ValueError
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        sealed_capability = str(payload["capability"])
        if not is_sealed(sealed_capability):
            raise ValueError("dispatch capability must be sealed")
        input_result = payload.get("input_result")
        if input_result is not None:
            input_result = _INPUT_RESULT_ADAPTER.validate_python(input_result)
        input_result_digest = payload.get("input_result_digest")
        if input_result_digest is not None and not isinstance(input_result_digest, str):
            raise ValueError
        envelope = DispatchEnvelope(
            session_id=str(payload["session_id"]),
            runtime=str(payload["runtime"]),
            repository_id=int(payload["repository_id"]),
            pull_request_id=int(payload["pull_request_id"]),
            snapshot_id=int(payload["snapshot_id"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            capability=unwrap_capability(sealed_capability),
            capability_id=str(payload["capability_id"]),
            context_plan_fingerprint=str(payload["context_plan_fingerprint"]),
            role=parse_agent_investigation_role(str(payload["role"])),
            turn_budget=int(payload["turn_budget"]),
            timeout_seconds=int(payload["timeout_seconds"]),
            max_result_bytes=int(payload["max_result_bytes"]),
            diff_text=str(payload["diff_text"]),
            source_artifact=source_artifact,
            input_result=input_result,
            input_result_digest=input_result_digest,
            expires_at=expires_at,
        )
    except (InvalidSignature, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DispatchEnvelopeError("dispatch envelope is invalid") from error
    if envelope.expires_at <= (now or datetime.now(UTC)).astimezone(UTC):
        raise DispatchEnvelopeError("dispatch envelope is expired")
    _validate_envelope(envelope)
    return envelope
