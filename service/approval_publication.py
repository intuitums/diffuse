"""Provider-neutral automatic-approval publication results and stale-state errors."""

from __future__ import annotations

from dataclasses import dataclass


class ApprovalNotCurrentError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PublishedApproval:
    external_id: str
    external_url: str | None
