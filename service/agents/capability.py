"""Short-lived session capabilities for the isolated agent runner.

The control plane mints a capability; the runner presents it when calling
scoped, read-only Diffuse tools. A capability is not a Diffuse service token
and never carries database or GitHub App credentials.

This module is the Gate A contract. Production minting and the HTTP tool
endpoint wire into it in Gate B; nothing here executes a CLI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from service.review.runtimes import CLAUDE_CODE_RUNTIME, CODEX_RUNTIME

AgentRuntimeName = Literal["claude", "codex"]
AgentProfileName = Literal["answer", "review", "verify", "learn"]

#: Every tool operation a capability may name. Gate B's tool endpoint refuses
#: anything outside this set even if a caller minted a wider claim.
CAPABILITY_OPERATIONS: frozenset[str] = frozenset(
    {
        "search_code",
        "read_diff",
        "read_file",
        "read_symbol",
        "read_pr_metadata",
        "read_review_threads",
        "read_prior_findings",
        "read_policy",
    }
)

RUNTIME_NAMES: frozenset[str] = frozenset(
    {CLAUDE_CODE_RUNTIME, CODEX_RUNTIME}
)
PROFILE_NAMES: frozenset[str] = frozenset({"answer", "review", "verify", "learn"})

DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_REQUESTS = 64
MAX_TTL_SECONDS = 60 * 60
MAX_MAX_REQUESTS = 512
_TOKEN_SEPARATOR = "."


class CapabilityError(ValueError):
    """A capability is missing, expired, malformed, or out of scope."""


@dataclass(frozen=True)
class SessionCapability:
    """The claims a runner may exercise for one agent session."""

    capability_id: str
    runtime: AgentRuntimeName
    profile: AgentProfileName
    repository_id: int
    snapshot_id: int
    pull_request_number: int | None
    head_sha: str
    operations: frozenset[str]
    max_requests: int
    expires_at: datetime

    def allows(self, operation: str) -> bool:
        return operation in self.operations

    def as_audit_dict(self) -> dict[str, Any]:
        """Return a credential-free dict safe for audit logs."""

        return {
            "capability_id": self.capability_id,
            "runtime": self.runtime,
            "profile": self.profile,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "operations": sorted(self.operations),
            "max_requests": self.max_requests,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True)
class MintedCapability:
    """Bearer token plus the claims it encodes."""

    token: str
    capability: SessionCapability


def _require_utc(moment: datetime, *, field: str) -> datetime:
    if moment.tzinfo is None:
        raise CapabilityError(f"{field} must be timezone-aware")
    return moment.astimezone(UTC)


def _validate_operations(operations: frozenset[str] | set[str] | tuple[str, ...]) -> frozenset[str]:
    selected = frozenset(operations)
    if not selected:
        raise CapabilityError("capability operations must not be empty")
    unknown = selected - CAPABILITY_OPERATIONS
    if unknown:
        raise CapabilityError(
            "capability operations contain unknown values: "
            + ", ".join(sorted(unknown))
        )
    return selected


def _payload_bytes(capability: SessionCapability) -> bytes:
    body = {
        "capability_id": capability.capability_id,
        "runtime": capability.runtime,
        "profile": capability.profile,
        "repository_id": capability.repository_id,
        "snapshot_id": capability.snapshot_id,
        "pull_request_number": capability.pull_request_number,
        "head_sha": capability.head_sha,
        "operations": sorted(capability.operations),
        "max_requests": capability.max_requests,
        "expires_at": capability.expires_at.astimezone(UTC).isoformat(),
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def _sign(payload: bytes, *, secret: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def mint_session_capability(
    *,
    secret: bytes,
    runtime: str,
    profile: str,
    repository_id: int,
    snapshot_id: int,
    head_sha: str,
    operations: frozenset[str] | set[str] | tuple[str, ...],
    pull_request_number: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    now: datetime | None = None,
    capability_id: str | None = None,
) -> MintedCapability:
    """Mint a bearer capability for one isolated agent session."""

    if not secret:
        raise CapabilityError("capability secret must not be empty")
    if runtime not in RUNTIME_NAMES:
        raise CapabilityError(
            f"runtime must be one of: {', '.join(sorted(RUNTIME_NAMES))}"
        )
    if profile not in PROFILE_NAMES:
        raise CapabilityError(
            f"profile must be one of: {', '.join(sorted(PROFILE_NAMES))}"
        )
    if repository_id < 1:
        raise CapabilityError("repository_id must be positive")
    if snapshot_id < 1:
        raise CapabilityError("snapshot_id must be positive")
    if pull_request_number is not None and pull_request_number < 1:
        raise CapabilityError("pull_request_number must be positive when set")
    sha = head_sha.strip().lower()
    if len(sha) < 7 or any(char not in "0123456789abcdef" for char in sha):
        raise CapabilityError("head_sha must be a hex git object id")
    if ttl_seconds < 1 or ttl_seconds > MAX_TTL_SECONDS:
        raise CapabilityError(
            f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}"
        )
    if max_requests < 1 or max_requests > MAX_MAX_REQUESTS:
        raise CapabilityError(
            f"max_requests must be between 1 and {MAX_MAX_REQUESTS}"
        )

    moment = _require_utc(now or datetime.now(UTC), field="now")
    capability = SessionCapability(
        capability_id=capability_id or secrets.token_urlsafe(16),
        runtime=cast(AgentRuntimeName, runtime),
        profile=cast(AgentProfileName, profile),
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        pull_request_number=pull_request_number,
        head_sha=sha,
        operations=_validate_operations(operations),
        max_requests=max_requests,
        expires_at=moment + timedelta(seconds=ttl_seconds),
    )
    payload = _payload_bytes(capability)
    token = (
        f"{capability.capability_id}{_TOKEN_SEPARATOR}"
        f"{payload.hex()}{_TOKEN_SEPARATOR}"
        f"{_sign(payload, secret=secret)}"
    )
    return MintedCapability(token=token, capability=capability)


def verify_session_capability(
    token: str,
    *,
    secret: bytes,
    now: datetime | None = None,
    required_operation: str | None = None,
    repository_id: int | None = None,
    snapshot_id: int | None = None,
) -> SessionCapability:
    """Validate a bearer capability and optionally pin its scope."""

    if not secret:
        raise CapabilityError("capability secret must not be empty")
    parts = token.split(_TOKEN_SEPARATOR)
    if len(parts) != 3:
        raise CapabilityError("capability token is malformed")
    capability_id, payload_hex, signature = parts
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError as error:
        raise CapabilityError("capability token payload is malformed") from error
    expected = _sign(payload, secret=secret)
    if not hmac.compare_digest(expected, signature):
        raise CapabilityError("capability token signature is invalid")
    try:
        body = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapabilityError("capability token payload is malformed") from error
    if not isinstance(body, dict):
        raise CapabilityError("capability token payload is malformed")
    if body.get("capability_id") != capability_id:
        raise CapabilityError("capability token identity is inconsistent")

    try:
        expires_at = datetime.fromisoformat(str(body["expires_at"]))
        operations = _validate_operations(body["operations"])
        capability = SessionCapability(
            capability_id=str(body["capability_id"]),
            runtime=cast(AgentRuntimeName, str(body["runtime"])),
            profile=cast(AgentProfileName, str(body["profile"])),
            repository_id=int(body["repository_id"]),
            snapshot_id=int(body["snapshot_id"]),
            pull_request_number=(
                None
                if body.get("pull_request_number") is None
                else int(body["pull_request_number"])
            ),
            head_sha=str(body["head_sha"]),
            operations=operations,
            max_requests=int(body["max_requests"]),
            expires_at=_require_utc(expires_at, field="expires_at"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CapabilityError("capability token claims are malformed") from error

    if capability.runtime not in RUNTIME_NAMES:
        raise CapabilityError(f"capability runtime {capability.runtime!r} is unsupported")
    if capability.profile not in PROFILE_NAMES:
        raise CapabilityError(f"capability profile {capability.profile!r} is unsupported")

    moment = _require_utc(now or datetime.now(UTC), field="now")
    if moment >= capability.expires_at:
        raise CapabilityError("capability token has expired")
    if required_operation is not None and not capability.allows(required_operation):
        raise CapabilityError(
            f"capability does not allow operation {required_operation!r}"
        )
    if repository_id is not None and capability.repository_id != repository_id:
        raise CapabilityError("capability repository_id does not match the request")
    if snapshot_id is not None and capability.snapshot_id != snapshot_id:
        raise CapabilityError("capability snapshot_id does not match the request")
    return capability


@dataclass
class CapabilityRequestBudget:
    """Tracks request spend for one minted capability.

    Gate B's tool endpoint owns an instance per session. Kept here so the
    budget rule is unit-tested with the rest of the contract.
    """

    capability: SessionCapability
    requests_used: int = 0

    def consume(self) -> None:
        if self.requests_used >= self.capability.max_requests:
            raise CapabilityError("capability request budget is exhausted")
        self.requests_used += 1
