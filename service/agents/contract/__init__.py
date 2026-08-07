"""Shared CLI-native agent operation contract.

Diffuse's control plane (API + worker) and the isolated agent-runner share these
modules. The worker mints session capabilities and validates structured results;
the runner is the only process that executes a CLI or mounts agent credentials.

See `docs/agent-runtimes.md` and the Linear document
"CLI-native agent operation plan".
"""

from service.agents.contract.capability import (
    CAPABILITY_OPERATIONS,
    CapabilityError,
    CapabilityExpired,
    CapabilityMalformed,
    CapabilityScopeMismatch,
    SessionCapability,
    SessionCapabilityGrant,
    SessionScope,
    mint_session_capability,
    verify_session_capability,
)
from service.agents.contract.result import (
    RESULT_SCHEMA_VERSION,
    AgentFinding,
    AgentSessionResult,
    ResultValidationError,
    ResultValidationFailureCode,
    validate_agent_session_result,
)
from service.agents.contract.runtime import (
    AGENT_RUNTIME_CLAUDE,
    AGENT_RUNTIME_CODEX,
    AGENT_RUNTIME_NAMES,
    AgentRuntimeConfig,
    parse_agent_runtime_name,
)

__all__ = [
    "AGENT_RUNTIME_CLAUDE",
    "AGENT_RUNTIME_CODEX",
    "AGENT_RUNTIME_NAMES",
    "CAPABILITY_OPERATIONS",
    "RESULT_SCHEMA_VERSION",
    "AgentFinding",
    "AgentRuntimeConfig",
    "AgentSessionResult",
    "CapabilityError",
    "CapabilityExpired",
    "CapabilityMalformed",
    "CapabilityScopeMismatch",
    "ResultValidationError",
    "ResultValidationFailureCode",
    "SessionCapability",
    "SessionCapabilityGrant",
    "SessionScope",
    "mint_session_capability",
    "parse_agent_runtime_name",
    "validate_agent_session_result",
    "verify_session_capability",
]
