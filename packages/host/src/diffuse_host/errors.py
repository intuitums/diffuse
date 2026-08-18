"""Failure classes for an agent session.

`ValueError` is intentionally reserved for failures a workflow must not retry:
the hosted worker already gives that base class terminal semantics.
"""

from __future__ import annotations


class AgentInvestigationError(RuntimeError):
    """A transient session failure; the workflow may retry the review."""


class AgentInvestigationTerminalError(ValueError):
    """A configuration or authentication failure no retry can repair."""


class AgentInvestigationAuthRequired(AgentInvestigationTerminalError):
    """Vendor refresh/login was rejected; an operator must reconnect."""


class AgentInvestigationTimeout(AgentInvestigationError):
    """The agent did not exit before the profile's deadline."""


class AgentInvestigationExecutionError(AgentInvestigationError):
    """The CLI reported an error while it was executing a turn."""


class AgentInvestigationRateLimited(AgentInvestigationError):
    """The vendor reported a rate or seat-quota limit."""


class AgentInvestigationOutputError(AgentInvestigationError):
    """The CLI output was not a valid structured response."""


class AgentInvestigationMcpError(AgentInvestigationError):
    """A configured MCP tool server failed to start."""


class AgentInvestigationCoverageCaveat(AgentInvestigationError):
    """The agent stopped at its turn budget rather than at an answer.

    An adapter can convert this into a report caveat rather than a failed
    review.  It remains distinct from a terminal error so it is never silently
    classified as a bad configuration.

    Carries the usage the abandoned attempt spent, because the vendor bills for
    it either way: a retry that reported only its own tokens would understate
    the review's cost by however far the first attempt got.
    """

    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    @property
    def usage(self) -> tuple[int, int]:
        return self.prompt_tokens, self.completion_tokens
