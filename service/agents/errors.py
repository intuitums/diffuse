"""Failure classes for an agent session.

`ValueError` is intentionally reserved for failures a workflow must not retry:
the hosted worker already gives that base class terminal semantics.
"""

from __future__ import annotations


class AgentSessionError(RuntimeError):
    """A transient session failure; the workflow may retry the review."""


class AgentSessionTerminalError(ValueError):
    """A configuration or authentication failure no retry can repair."""


class AgentSessionTimeout(AgentSessionError):
    """The agent did not exit before the profile's deadline."""


class AgentSessionExecutionError(AgentSessionError):
    """The CLI reported an error while it was executing a turn."""


class AgentSessionRateLimited(AgentSessionError):
    """The vendor reported a rate or seat-quota limit."""


class AgentSessionOutputError(AgentSessionError):
    """The CLI output was not a valid structured response."""


class AgentSessionMcpError(AgentSessionError):
    """A configured MCP tool server failed to start."""


class AgentSessionCoverageCaveat(AgentSessionError):
    """The raised-budget retry also exhausted the agent's turn budget.

    An adapter can convert this into a report caveat rather than a failed
    review.  It remains distinct from a terminal error so it is never silently
    classified as a bad configuration.
    """
