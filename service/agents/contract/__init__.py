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
    AgentInvestigationRole,
    AgentRuntimeConfig,
    opposite_agent_runtime,
    parse_agent_investigation_role,
    parse_agent_runtime_name,
)
from service.agents.contract.result import (
    RESULT_SCHEMA_VERSION,
    VERIFICATION_RESULT_SCHEMA_VERSION,
    AgentFinding,
    AgentInvestigationResult,
    AgentVerificationResult,
    ResultValidationError,
    ResultValidationFailureCode,
    accept_bound_agent_investigation_result,
    accept_bound_agent_verification_result,
    accept_candidate_result_input,
    canonical_result_payload_bytes,
    canonical_result_payload_digest,
    validate_agent_investigation_result,
    validate_agent_verification_result,
)

__all__ = [
    "AGENT_RUNTIME_CLAUDE",
    "AGENT_RUNTIME_CODEX",
    "AGENT_RUNTIME_NAMES",
    "CAPABILITY_OPERATIONS",
    "DEFAULT_MAX_RESULT_BYTES",
    "AgentInvestigationRole",
    "MAX_CAPABILITY_TTL",
    "RESULT_SCHEMA_VERSION",
    "VERIFICATION_RESULT_SCHEMA_VERSION",
    "AgentFinding",
    "AgentRuntimeConfig",
    "opposite_agent_runtime",
    "parse_agent_investigation_role",
    "AgentInvestigationResult",
    "AgentVerificationResult",
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
    "accept_bound_agent_verification_result",
    "accept_candidate_result_input",
    "canonical_result_payload_bytes",
    "canonical_result_payload_digest",
    "mint_session_capability",
    "parse_agent_runtime_name",
    "validate_agent_investigation_result",
    "validate_agent_verification_result",
    "verify_session_capability",
]
