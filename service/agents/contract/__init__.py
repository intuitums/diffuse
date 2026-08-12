"""Shared CLI-native agent operation contract.

Diffuse's control plane (API + worker) and the isolated agent-host share these
modules. The worker mints session capabilities and validates structured results;
the runner is the only process that executes a CLI or mounts agent credentials.

See `docs/agents.md` and the Linear document
"CLI-native agent operation plan".
"""

from service.agents.contract.access_grant import (
    CAPABILITY_OPERATIONS,
    MAX_CAPABILITY_TTL,
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
from service.agents.contract.agent import (
    AGENT_RUNTIME_CLAUDE,
    AGENT_RUNTIME_CODEX,
    AGENT_RUNTIME_NAMES,
    DEFAULT_MAX_RESULT_BYTES,
    AgentRuntimeConfig,
    parse_agent_runtime_name,
)
from service.agents.contract.result import (
    RESULT_SCHEMA_VERSION,
    AgentFinding,
    AgentInvestigationResult,
    ResultValidationError,
    ResultValidationFailureCode,
    accept_bound_agent_investigation_result,
    validate_agent_investigation_result,
)

__all__ = [
    "AGENT_RUNTIME_CLAUDE",
    "AGENT_RUNTIME_CODEX",
    "AGENT_RUNTIME_NAMES",
    "CAPABILITY_OPERATIONS",
    "DEFAULT_MAX_RESULT_BYTES",
    "MAX_CAPABILITY_TTL",
    "RESULT_SCHEMA_VERSION",
    "AgentFinding",
    "AgentRuntimeConfig",
    "AgentInvestigationResult",
    "CapabilityError",
    "CapabilityExpired",
    "CapabilityMalformed",
    "CapabilityScopeMismatch",
    "ResultValidationError",
    "ResultValidationFailureCode",
    "SessionCapability",
    "SessionCapabilityGrant",
    "SessionScope",
    "accept_bound_agent_investigation_result",
    "mint_session_capability",
    "parse_agent_runtime_name",
    "validate_agent_investigation_result",
    "verify_session_capability",
]
