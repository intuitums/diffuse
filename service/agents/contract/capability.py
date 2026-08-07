"""Short-lived session capabilities for the isolated agent-runner.

The control plane mints a capability pinned to a repository, pull request,
index snapshot, and read-only operation allowlist. The runner presents the
token when calling Diffuse tool endpoints. The worker never executes a CLI and
never mounts agent credentials; it only mints and later validates results.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from service.agents.contract.runtime import AGENT_RUNTIME_NAMES, parse_agent_runtime_name

#: Read-only operations a session capability may authorize. Mutating GitHub or
#: database operations are intentionally absent; Diffuse alone publishes.
CAPABILITY_OPERATIONS: frozenset[str] = frozenset(
    {
        "search_code",
        "get_diff",
        "get_file",
        "get_symbol",
        "get_pr_metadata",
        "list_prior_findings",
        "get_applicable_policy",
    }
)

DEFAULT_CAPABILITY_TTL = timedelta(minutes=15)
#: A caller may choose a shorter session, never a longer-lived bearer token.
#: Keeping this equal to the documented default makes "short-lived" a contract,
#: not merely the value most call sites happen to use.
MAX_CAPABILITY_TTL = DEFAULT_CAPABILITY_TTL
_TOKEN_VERSION = 1


class CapabilityError(ValueError):
    """Base class for session-capability contract failures."""


class CapabilityMalformed(CapabilityError):
    """The token is not a well-formed Diffuse session capability."""


class CapabilityExpired(CapabilityError):
    """The capability's expiry has passed."""


class CapabilityScopeMismatch(CapabilityError):
    """The capability does not authorize the requested scope or operation."""


@dataclass(frozen=True)
class SessionScope:
    """The repository/PR/snapshot pin and allowed operations for one session."""

    repository_id: int
    pull_request_id: int
    snapshot_id: int
    head_sha: str
    operations: frozenset[str]

    def __post_init__(self) -> None:
        if self.repository_id <= 0:
            raise ValueError("repository_id must be positive")
        if self.pull_request_id <= 0:
            raise ValueError("pull_request_id must be positive")
        if self.snapshot_id <= 0:
            raise ValueError("snapshot_id must be positive")
        sha = self.head_sha.strip().lower()
        # Pull-request heads in the control plane are full object IDs. Accepting
        # an abbreviated SHA here minted a token that later failed the exact
        # database comparison in the capability tool, and would make an
        # "exact" session pin collision-prone if that comparison changed.
        if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
            raise ValueError("head_sha must be a full 40-character hexadecimal git commit")
        object.__setattr__(self, "head_sha", sha)
        unknown = sorted(self.operations - CAPABILITY_OPERATIONS)
        if unknown:
            raise ValueError(
                "operations contains unsupported values: " + ", ".join(unknown)
            )
        if not self.operations:
            raise ValueError("operations must authorize at least one read-only tool")


@dataclass(frozen=True)
class SessionCapability:
    """A verified session capability (never carries the raw token secret)."""

    capability_id: str
    runtime: str
    scope: SessionScope
    issued_at: datetime
    expires_at: datetime

    def allows(self, operation: str) -> bool:
        return operation in self.scope.operations


@dataclass(frozen=True)
class SessionCapabilityGrant:
    """The one-time mint result: bearer token plus verified capability metadata."""

    token: str
    capability: SessionCapability


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def mint_session_capability(
    *,
    signing_key: bytes | str,
    runtime: str,
    scope: SessionScope,
    ttl: timedelta = DEFAULT_CAPABILITY_TTL,
    now: datetime | None = None,
    capability_id: str | None = None,
) -> SessionCapabilityGrant:
    """Mint a short-lived, scope-pinned capability token for the agent-runner."""

    if not signing_key:
        raise ValueError("signing_key must be non-empty")
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if ttl > MAX_CAPABILITY_TTL:
        raise ValueError(
            f"ttl must not exceed {int(MAX_CAPABILITY_TTL.total_seconds())} seconds"
        )
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    selected_runtime = parse_agent_runtime_name(runtime)
    issued_at = _as_utc(now or datetime.now(UTC))
    expires_at = issued_at + ttl
    token_id = capability_id or secrets.token_urlsafe(16)
    payload = {
        "v": _TOKEN_VERSION,
        "jti": token_id,
        "runtime": selected_runtime,
        "repository_id": scope.repository_id,
        "pull_request_id": scope.pull_request_id,
        "snapshot_id": scope.snapshot_id,
        "head_sha": scope.head_sha,
        "operations": sorted(scope.operations),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64url_encode(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    token = f"diffuse-cap.{body}.{signature}"
    capability = SessionCapability(
        capability_id=token_id,
        runtime=selected_runtime,
        scope=scope,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return SessionCapabilityGrant(token=token, capability=capability)


def _parse_payload(token: str, signing_key: bytes) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "diffuse-cap":
        raise CapabilityMalformed("session capability token is malformed")
    _, body, signature = parts
    expected = _b64url_encode(hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise CapabilityMalformed("session capability signature is invalid")
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError) as error:
        raise CapabilityMalformed("session capability payload is not valid JSON") from error
    if not isinstance(payload, dict):
        raise CapabilityMalformed("session capability payload must be an object")
    return payload


def verify_session_capability(
    token: str,
    *,
    signing_key: bytes | str,
    now: datetime | None = None,
    require_operation: str | None = None,
    require_repository_id: int | None = None,
    require_pull_request_id: int | None = None,
    require_snapshot_id: int | None = None,
    require_head_sha: str | None = None,
    require_runtime: str | None = None,
) -> SessionCapability:
    """Verify a minted capability and optionally enforce a requested scope."""

    if not signing_key:
        raise ValueError("signing_key must be non-empty")
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    payload = _parse_payload(token, key)
    try:
        version = payload["v"]
        token_id = payload["jti"]
        runtime = payload["runtime"]
        repository_id = payload["repository_id"]
        pull_request_id = payload["pull_request_id"]
        snapshot_id = payload["snapshot_id"]
        head_sha = payload["head_sha"]
        operations = frozenset(payload["operations"])
        issued_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
        expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
    except (KeyError, TypeError, ValueError) as error:
        raise CapabilityMalformed("session capability payload is incomplete") from error
    if version != _TOKEN_VERSION:
        raise CapabilityMalformed(f"unsupported session capability version {version!r}")
    if not isinstance(token_id, str) or not token_id:
        raise CapabilityMalformed("session capability id is invalid")
    if runtime not in AGENT_RUNTIME_NAMES:
        raise CapabilityMalformed(f"session capability runtime {runtime!r} is unknown")
    try:
        scope = SessionScope(
            repository_id=int(repository_id),
            pull_request_id=int(pull_request_id),
            snapshot_id=int(snapshot_id),
            head_sha=str(head_sha),
            operations=operations,
        )
    except (TypeError, ValueError) as error:
        raise CapabilityMalformed(str(error)) from error

    moment = _as_utc(now or datetime.now(UTC))
    if moment >= expires_at:
        raise CapabilityExpired(
            f"session capability {token_id} expired at {expires_at.isoformat()}"
        )

    capability = SessionCapability(
        capability_id=token_id,
        runtime=runtime,
        scope=scope,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    if require_runtime is not None and capability.runtime != parse_agent_runtime_name(
        require_runtime
    ):
        raise CapabilityScopeMismatch(
            f"session capability runtime is {capability.runtime!r}, "
            f"required {require_runtime!r}"
        )
    if (
        require_repository_id is not None
        and capability.scope.repository_id != require_repository_id
    ):
        raise CapabilityScopeMismatch("session capability repository_id does not match")
    if (
        require_pull_request_id is not None
        and capability.scope.pull_request_id != require_pull_request_id
    ):
        raise CapabilityScopeMismatch("session capability pull_request_id does not match")
    if require_snapshot_id is not None and capability.scope.snapshot_id != require_snapshot_id:
        raise CapabilityScopeMismatch("session capability snapshot_id does not match")
    if (
        require_head_sha is not None
        and capability.scope.head_sha != require_head_sha.strip().lower()
    ):
        raise CapabilityScopeMismatch("session capability head_sha does not match")
    if require_operation is not None:
        if require_operation not in CAPABILITY_OPERATIONS:
            raise CapabilityScopeMismatch(
                f"operation {require_operation!r} is not a Diffuse capability tool"
            )
        if not capability.allows(require_operation):
            raise CapabilityScopeMismatch(
                f"session capability does not authorize {require_operation!r}"
            )
    return capability
