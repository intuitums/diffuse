"""Tools a review runtime can call over a pinned context plan.

Review Agents investigate through these private, capability-scoped tools.
They share `search_codebase` with the Context Service, so every investigation
receives the same index-backed answers.

Every call is appended to `review_tool_calls` through a recorder. The worker
uses a Postgres-backed recorder once it has a durable review run.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from retriever.context_models import CrossRepositoryContextPlan
from service.code_query import CodeQueryTarget, search_codebase
from service.review.tool_log import ReviewToolCallHandle, record_review_tool_call

SEARCH_CODE_TOOL = "search_code"


@dataclass(frozen=True)
class RecordedToolCall:
    """One investigation step, whether or not it was persisted to Postgres."""

    tool_name: str
    arguments: dict[str, object]
    duration_ms: int
    result: dict[str, object] | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    index_snapshot_ids: tuple[int, ...] = ()
    context_plan_fingerprint: str | None = None
    handle: ReviewToolCallHandle | None = None


class ReviewToolRecorder(Protocol):
    """Receives tool calls a runtime made during one review attempt."""

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        duration_ms: int,
        result: dict[str, object] | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        index_snapshot_ids: tuple[int, ...] = (),
        context_plan_fingerprint: str | None = None,
    ) -> RecordedToolCall:
        """Append one succeeded or failed call."""


@dataclass
class MemoryToolRecorder:
    """Keeps calls in process memory — local CLI and unit tests."""

    calls: list[RecordedToolCall] = field(default_factory=list)

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        duration_ms: int,
        result: dict[str, object] | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        index_snapshot_ids: tuple[int, ...] = (),
        context_plan_fingerprint: str | None = None,
    ) -> RecordedToolCall:
        entry = RecordedToolCall(
            tool_name=tool_name,
            arguments=arguments,
            duration_ms=duration_ms,
            result=result,
            failure_code=failure_code,
            failure_detail=failure_detail,
            index_snapshot_ids=index_snapshot_ids,
            context_plan_fingerprint=context_plan_fingerprint,
        )
        self.calls.append(entry)
        return entry


@dataclass
class PostgresToolRecorder:
    """Persists calls onto an existing `review_runs` row."""

    conn: object
    review_run_id: int
    attempt_started_at: datetime | None = None
    calls: list[RecordedToolCall] = field(default_factory=list)

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        duration_ms: int,
        result: dict[str, object] | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        index_snapshot_ids: tuple[int, ...] = (),
        context_plan_fingerprint: str | None = None,
    ) -> RecordedToolCall:
        handle = record_review_tool_call(
            self.conn,
            self.review_run_id,
            tool_name=tool_name,
            arguments=arguments,
            duration_ms=duration_ms,
            result=result,
            failure_code=failure_code,
            failure_detail=failure_detail,
            index_snapshot_ids=index_snapshot_ids,
            context_plan_fingerprint=context_plan_fingerprint,
            attempt_started_at=self.attempt_started_at,
        )
        entry = RecordedToolCall(
            tool_name=tool_name,
            arguments=arguments,
            duration_ms=duration_ms,
            result=result,
            failure_code=failure_code,
            failure_detail=failure_detail,
            index_snapshot_ids=index_snapshot_ids,
            context_plan_fingerprint=context_plan_fingerprint,
            handle=handle,
        )
        self.calls.append(entry)
        return entry


def _snapshot_ids(plan: CrossRepositoryContextPlan) -> tuple[int, ...]:
    ids: list[int] = []
    if plan.primary_snapshot_id is not None:
        ids.append(plan.primary_snapshot_id)
    ids.extend(item.snapshot_id for item in plan.related_snapshots)
    return tuple(ids)


@dataclass
class ReviewToolProvider:
    """Index-backed tools for one review against a pinned context plan."""

    target: CodeQueryTarget
    recorder: ReviewToolRecorder = field(default_factory=MemoryToolRecorder)

    @property
    def context_plan(self) -> CrossRepositoryContextPlan:
        return self.target.context_plan

    @property
    def recorded_calls(self) -> Sequence[RecordedToolCall]:
        return tuple(self.recorder.calls)

    def search_code(
        self,
        query: str,
        *,
        path_prefix: str | None = None,
        limit: int = 8,
    ) -> dict[str, object]:
        """Search the pinned snapshots; always record the attempt."""

        arguments: dict[str, object] = {
            "query": query,
            "path": path_prefix,
            "limit": limit,
        }
        started = time.perf_counter()
        snapshot_ids = _snapshot_ids(self.target.context_plan)
        fingerprint = self.target.context_plan.fingerprint or None
        try:
            result = search_codebase(
                self.target,
                query=query,
                path_prefix=path_prefix,
                limit=limit,
            )
        except Exception as error:
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            detail = str(error).strip() or error.__class__.__name__
            self.recorder.record(
                tool_name=SEARCH_CODE_TOOL,
                arguments=arguments,
                duration_ms=duration_ms,
                failure_code="search_failed",
                failure_detail=detail[:2000],
                index_snapshot_ids=snapshot_ids,
                context_plan_fingerprint=fingerprint,
            )
            raise
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        self.recorder.record(
            tool_name=SEARCH_CODE_TOOL,
            arguments=arguments,
            duration_ms=duration_ms,
            result=result,
            index_snapshot_ids=snapshot_ids,
            context_plan_fingerprint=fingerprint,
        )
        return result


def build_review_tool_provider(
    target: CodeQueryTarget,
    *,
    recorder: ReviewToolRecorder | None = None,
) -> ReviewToolProvider:
    """Construct a provider; defaults to an in-memory recorder."""

    return ReviewToolProvider(
        target=target,
        recorder=recorder if recorder is not None else MemoryToolRecorder(),
    )
