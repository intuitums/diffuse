"""The bounded ways Diffuse asks an agent CLI to work."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionProfile:
    """Resource and tool policy for one kind of agent turn.

    Keeping these values here makes the session runner independent from review,
    Q&A, conversation, and learning.  Future consumers select a profile rather
    than growing their own subprocess policy.
    """

    name: str
    turn_budget: int
    timeout_seconds: int
    tool_allowlist: tuple[str, ...] = ("search_code",)
    needs_workspace: bool = True

    def __post_init__(self) -> None:
        if self.turn_budget <= 0:
            raise ValueError("Agent session turn budget must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("Agent session timeout must be positive")

    def with_raised_budget(self) -> SessionProfile:
        """Return the one permitted retry shape for Claude's max-turn error."""

        return SessionProfile(
            name=self.name,
            turn_budget=self.turn_budget * 2,
            timeout_seconds=self.timeout_seconds,
            tool_allowlist=self.tool_allowlist,
            needs_workspace=self.needs_workspace,
        )


ANSWER = SessionProfile("answer", turn_budget=8, timeout_seconds=180)
REVIEW = SessionProfile("review", turn_budget=24, timeout_seconds=600)
VERIFY = SessionProfile("verify", turn_budget=12, timeout_seconds=300)
LEARN = SessionProfile("learn", turn_budget=12, timeout_seconds=300)
