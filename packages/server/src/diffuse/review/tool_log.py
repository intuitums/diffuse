"""Append-only record of the tool calls a review run made, and their results.

A review is reproducible today because the context it read is a pure function of
its pinned snapshots and its diff. An agentic reviewer picks its own queries, so
that stops being true the moment it can call `search_code` for
itself: the snapshots say what it *could* read, and only this log says what it
did read. Nothing here is derivable after the fact, which is why recording is not
optional and why a call that failed is still a row.

A review run outlives its attempts -- `begin_review_run` resets a failed or
generating run in place rather than creating a second one -- so every row is
filed under the attempt that made it, identified by the `review_runs.started_at`
that reset refreshes. The earlier attempts stay: knowing what the reviewer looked
at before it gave up is the same kind of evidence as knowing what it looked at
before it concluded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

import psycopg2
import psycopg2.errors
import psycopg2.extras

# Compact-canonical-JSON budgets, per payload.
#
# Arguments are a handful of model-authored strings; 8 KiB is the bound
# `review_depth_resolution` already uses for model-authored text and no honest
# `search_code` call comes near it. Results are the real risk: a single
# `search_code` reply carries up to 20 sources of 8,000 characters each, so an
# unbounded copy would put ~160 KB in a row that is written several times per
# review. 64 KiB is the bound `review_runs.provenance` already uses, and it sits
# comfortably above `MAX_ANSWER_CONTEXT_CHARS` (24,000) -- the cap on what the
# model is handed in the first place -- so the truncating path is the pathological
# case, not the normal one.
MAX_ARGUMENT_BYTES = 8_192
MAX_RESULT_BYTES = 65_536
MAX_INDEX_SNAPSHOT_IDS = 64
MAX_FAILURE_DETAIL_CHARS = 2_000
TRUNCATION_ENVELOPE_KEY = "diffuse_truncated_payload"
TRUNCATION_SCHEMA_VERSION = "diffuse-tool-payload-truncation-v1"
TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
FAILURE_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
# A losing ordinal race means two workers hold the same review-run id, which the
# workflow lease is supposed to prevent but a stale lease does not. Interleaved
# ordinals are a survivable outcome; a dropped call is not, so the append retries
# instead of returning.
APPEND_ATTEMPTS = 3
_SAVEPOINT = "diffuse_review_tool_call"


class ReviewToolLogError(RuntimeError):
    """A tool call could not be appended to the review's investigation log."""


@dataclass(frozen=True)
class BoundedPayload:
    """A payload as it will be stored, plus the identity of the one that arrived."""

    payload: dict[str, object]
    sha256: str
    byte_length: int
    truncated: bool


@dataclass(frozen=True)
class ReviewToolCallHandle:
    id: int
    ordinal: int
    # The attempt the call was filed under, resolved by the database when the
    # caller did not name one. Returned so a worker can tell that the run was
    # reset underneath it: an ordinal that went back to 1 is the symptom, and
    # this is the only value that says why.
    attempt_started_at: datetime
    arguments_truncated: bool
    result_truncated: bool

    @property
    def is_partial(self) -> bool:
        return self.arguments_truncated or self.result_truncated


@dataclass(frozen=True)
class ReviewToolCall:
    id: int
    # 1 for the run's first attempt, counting up in the order the attempts ran.
    # Derived from the ordering of `attempt_started_at` rather than stored, so it
    # cannot drift from the timestamps it summarises.
    attempt: int
    attempt_started_at: datetime
    # Whether this call belongs to the attempt the run is currently on -- for a
    # finished run, the attempt that produced the review that shipped. False for
    # every superseded attempt. No call is final while a reset has happened and
    # the new attempt has not logged anything yet, which is the honest answer.
    is_final_attempt: bool
    ordinal: int
    tool_name: str
    arguments: dict[str, object]
    arguments_truncated: bool
    arguments_sha256: str
    arguments_bytes: int
    status: str
    result: dict[str, object] | None
    result_truncated: bool
    result_sha256: str | None
    result_bytes: int | None
    failure_code: str | None
    failure_detail: str | None
    index_snapshot_ids: tuple[int, ...]
    context_plan_fingerprint: str | None
    duration_ms: int
    started_at: datetime

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def is_partial(self) -> bool:
        """Whether replaying this step compares against a payload we only kept part of."""
        return self.arguments_truncated or self.result_truncated


def _canonical_json(payload: dict[str, object]) -> str:
    """The exact bytes every digest and every size in this module is taken over.

    Sorted keys and no whitespace so that two runs of the same tool produce the
    same digest for the same answer, and `ensure_ascii` so the digest does not
    depend on the encoding the tool happened to emit.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _truncation_envelope(
    *,
    original_bytes: int,
    original_sha256: str,
    retained_prefix: str,
) -> dict[str, object]:
    return {
        TRUNCATION_ENVELOPE_KEY: {
            "schema_version": TRUNCATION_SCHEMA_VERSION,
            "original_bytes": original_bytes,
            "original_sha256": original_sha256,
            "retained_prefix": retained_prefix,
        }
    }


def bound_payload(payload: dict[str, object], *, limit: int) -> BoundedPayload:
    """Fit a tool payload inside `limit` bytes without ever pretending it is whole.

    What survives truncation is a prefix of the canonical *text*, not a pruned
    subtree. Pruning would need this module to know the shape of every tool's
    reply -- it deliberately does not, and a tool added later would silently get
    its most interesting field dropped. A text prefix also diffs directly against
    the canonical text of a replay, which is the operation a reader actually
    performs.

    The envelope carries the digest and size of the payload that arrived, so a
    replay can still prove equality against a copy that is only partial, and can
    say so when it cannot. A silently-differing replay is worse than an admittedly
    partial one.
    """
    if not isinstance(payload, dict):
        raise ValueError("Review tool payloads must be JSON objects")
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError) as error:
        raise ValueError("Review tool payload is not JSON-serializable") from error
    encoded = canonical.encode()
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) <= limit:
        return BoundedPayload(
            payload=payload,
            sha256=digest,
            byte_length=len(encoded),
            truncated=False,
        )

    envelope = _truncation_envelope(
        original_bytes=len(encoded),
        original_sha256=digest,
        retained_prefix="",
    )
    # The prefix is re-escaped when it goes back into JSON, so a payload dense in
    # quotes or backslashes costs about two bytes per retained character. Budget
    # from the measured size rather than from `limit - overhead`, or exactly the
    # payloads most in need of truncation overflow the limit again.
    budget = max(0, limit - len(_canonical_json(envelope).encode()))
    while True:
        envelope = _truncation_envelope(
            original_bytes=len(encoded),
            original_sha256=digest,
            retained_prefix=canonical[:budget],
        )
        overflow = len(_canonical_json(envelope).encode()) - limit
        if overflow <= 0 or budget == 0:
            break
        budget = max(0, budget - overflow)
    return BoundedPayload(
        payload=envelope,
        sha256=digest,
        byte_length=len(encoded),
        truncated=True,
    )


def _validated_snapshot_ids(index_snapshot_ids) -> list[int]:
    ids = [int(value) for value in index_snapshot_ids]
    if any(value <= 0 for value in ids) or len(ids) > MAX_INDEX_SNAPSHOT_IDS:
        raise ValueError("Review tool call snapshot IDs must be positive and bounded")
    return ids


_INSERT_TOOL_CALL = """
    INSERT INTO review_tool_calls (
        review_run_id,
        attempt_started_at,
        ordinal,
        tool_name,
        arguments,
        arguments_truncated,
        arguments_sha256,
        arguments_bytes,
        status,
        result,
        result_truncated,
        result_sha256,
        result_bytes,
        failure_code,
        failure_detail,
        index_snapshot_ids,
        context_plan_fingerprint,
        duration_ms,
        started_at
    )
    SELECT
        attempt.review_run_id,
        attempt.started_at,
        COALESCE(MAX(existing.ordinal), 0) + 1,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        COALESCE(%s::TIMESTAMPTZ, now() - make_interval(secs => %s / 1000.0))
    FROM (
        -- The attempt key comes from the run row itself, so a caller that passes
        -- nothing still files its call under the attempt the run is on. A run
        -- that no longer exists selects no rows and inserts nothing, which the
        -- caller is told about rather than the foreign key raising.
        SELECT id AS review_run_id, COALESCE(%s::TIMESTAMPTZ, started_at) AS started_at
        FROM review_runs
        WHERE id = %s
    ) AS attempt
    LEFT JOIN review_tool_calls AS existing
           ON existing.review_run_id = attempt.review_run_id
          -- Restricting the maximum to one attempt is what restarts the step
          -- numbering after a retry instead of continuing the failed attempt's.
          AND existing.attempt_started_at = attempt.started_at
    GROUP BY attempt.review_run_id, attempt.started_at
    RETURNING id, ordinal, attempt_started_at
"""


def record_review_tool_call(
    conn,
    review_run_id: int,
    *,
    tool_name: str,
    arguments: dict[str, object],
    duration_ms: int,
    result: dict[str, object] | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
    index_snapshot_ids: tuple[int, ...] = (),
    context_plan_fingerprint: str | None = None,
    started_at: datetime | None = None,
    attempt_started_at: datetime | None = None,
) -> ReviewToolCallHandle:
    """Append one tool call, successful or not, to a review run's investigation.

    A `failure_code` makes the row a failed call rather than an error: a review
    that concluded after three lookups that returned nothing reasoned from a
    different world than one whose lookups all answered, and only the log can
    tell the two apart afterwards.

    `attempt_started_at` names the attempt the call belongs to and defaults to
    whatever `review_runs.started_at` holds when the row is written, which is the
    value `begin_review_run` refreshes when it resets a failed run in place. A
    worker that already knows the `started_at` it began under should pass it: if
    the run was reset while a stale lease was still issuing calls, the default
    would file those late calls under the attempt that superseded them, and an
    explicit key keeps them with the attempt that actually made them.

    The write cannot damage the caller's transaction. Everything checkable is
    checked before any SQL runs, and the insert itself is wrapped in a savepoint
    so that a constraint the caller tripped -- a review run that no longer exists,
    a duplicate ordinal from a second worker -- rolls back the append alone. Left
    unguarded, a rejected log row would abort the surrounding transaction and take
    the review's findings with it, which inverts the point of the log entirely.
    """
    if not TOOL_NAME_PATTERN.fullmatch(tool_name):
        raise ValueError("Review tool name is invalid")
    if duration_ms < 0:
        raise ValueError("Review tool call duration cannot be negative")
    if failure_code is not None and not FAILURE_CODE_PATTERN.fullmatch(failure_code):
        raise ValueError("Review tool call failure code is invalid")
    if failure_detail is not None and not (
        failure_detail.strip() and len(failure_detail) <= MAX_FAILURE_DETAIL_CHARS
    ):
        raise ValueError("Review tool call failure detail must be bounded, non-empty text")
    if failure_code is None and result is None:
        raise ValueError("A review tool call must record a result or a failure code")
    if context_plan_fingerprint is not None and not FINGERPRINT_PATTERN.fullmatch(
        context_plan_fingerprint
    ):
        raise ValueError("Review tool call context plan fingerprint is invalid")
    snapshot_ids = _validated_snapshot_ids(index_snapshot_ids)

    bounded_arguments = bound_payload(arguments, limit=MAX_ARGUMENT_BYTES)
    bounded_result = (
        None if result is None else bound_payload(result, limit=MAX_RESULT_BYTES)
    )
    parameters = (
        tool_name,
        psycopg2.extras.Json(bounded_arguments.payload),
        bounded_arguments.truncated,
        bounded_arguments.sha256,
        bounded_arguments.byte_length,
        "failed" if failure_code is not None else "succeeded",
        None if bounded_result is None else psycopg2.extras.Json(bounded_result.payload),
        bounded_result is not None and bounded_result.truncated,
        None if bounded_result is None else bounded_result.sha256,
        None if bounded_result is None else bounded_result.byte_length,
        failure_code,
        failure_detail,
        snapshot_ids,
        context_plan_fingerprint,
        duration_ms,
        started_at,
        duration_ms,
        attempt_started_at,
        review_run_id,
    )

    # Named for the insert, not for the review's attempt: this loop retries one
    # row, and every pass files it under the same attempt of the same run.
    for insert_attempt in range(APPEND_ATTEMPTS):
        with conn.cursor() as cursor:
            cursor.execute(f"SAVEPOINT {_SAVEPOINT}")
            try:
                # Allocating the ordinal inside the INSERT leaves no window for a
                # second call in this transaction to read the same maximum.
                cursor.execute(_INSERT_TOOL_CALL, parameters)
                row = cursor.fetchone()
            except psycopg2.Error as error:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                if (
                    isinstance(error, psycopg2.errors.UniqueViolation)
                    and insert_attempt + 1 < APPEND_ATTEMPTS
                ):
                    continue
                raise ReviewToolLogError("Review tool call could not be recorded") from error
            if row is None:
                # The run row is where the attempt key comes from, so a run that
                # was deleted mid-review writes nothing instead of raising. Silent
                # success would be the worse outcome of the two: the caller would
                # believe the step was logged and a replay would be missing it.
                cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                raise ReviewToolLogError(
                    "Review tool call could not be recorded: the review run is gone"
                )
            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        return ReviewToolCallHandle(
            id=int(row[0]),
            ordinal=int(row[1]),
            attempt_started_at=row[2],
            arguments_truncated=bounded_arguments.truncated,
            result_truncated=bounded_result is not None and bounded_result.truncated,
        )
    raise ReviewToolLogError("Review tool call could not be recorded")


def load_review_tool_calls(
    conn,
    review_run_id: int,
    *,
    attempt: int | None = None,
) -> tuple[ReviewToolCall, ...]:
    """Replay order, which is attempt then `ordinal`, and never `id`.

    `id` comes from a sequence shared with every other run and gaps on rollback,
    so it agrees with the investigation's order only by accident of scheduling.
    Ordinals restart at 1 for every attempt, so they order a story only within
    one; a run that was retried has as many stories as it had attempts, and
    reading them as a single sequence is the ambiguity the attempt key removes.

    Every row says which attempt it came from and whether that attempt is the one
    the run is currently on. `attempt=N` narrows the read to the N-th attempt in
    the order they ran; the default returns all of them, oldest attempt first.
    """
    if attempt is not None and attempt < 1:
        raise ValueError("Review tool call attempt numbers start at 1")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            WITH attempts AS (
                SELECT
                    logged.*,
                    -- Numbered from the timestamps rather than stored alongside
                    -- them, so the two can never disagree about which attempt
                    -- came first.
                    DENSE_RANK() OVER (
                        ORDER BY logged.attempt_started_at
                    ) AS attempt,
                    -- The run's own `started_at` is the only authority on which
                    -- attempt is current. Taking the newest attempt in the log
                    -- instead would promote a superseded attempt the moment a
                    -- reset happened before the new attempt logged anything.
                    logged.attempt_started_at = run.started_at AS is_final_attempt
                FROM review_tool_calls AS logged
                JOIN review_runs AS run ON run.id = logged.review_run_id
                WHERE logged.review_run_id = %s
            )
            SELECT
                id,
                attempt,
                attempt_started_at,
                is_final_attempt,
                ordinal,
                tool_name,
                arguments,
                arguments_truncated,
                arguments_sha256,
                arguments_bytes,
                status,
                result,
                result_truncated,
                result_sha256,
                result_bytes,
                failure_code,
                failure_detail,
                index_snapshot_ids,
                context_plan_fingerprint,
                duration_ms,
                started_at
            FROM attempts
            WHERE %s::INTEGER IS NULL OR attempt = %s::INTEGER
            -- Attempt first: the ordinals of two attempts interleave, so
            -- ordering by them alone would splice two investigations together.
            ORDER BY attempt_started_at, ordinal
            """,
            (review_run_id, attempt, attempt),
        )
        rows = cursor.fetchall()
    return tuple(
        ReviewToolCall(
            id=int(row["id"]),
            attempt=int(row["attempt"]),
            attempt_started_at=row["attempt_started_at"],
            is_final_attempt=bool(row["is_final_attempt"]),
            ordinal=int(row["ordinal"]),
            tool_name=row["tool_name"],
            arguments=row["arguments"],
            arguments_truncated=bool(row["arguments_truncated"]),
            arguments_sha256=row["arguments_sha256"],
            arguments_bytes=int(row["arguments_bytes"]),
            status=row["status"],
            result=row["result"],
            result_truncated=bool(row["result_truncated"]),
            result_sha256=row["result_sha256"],
            result_bytes=(
                None if row["result_bytes"] is None else int(row["result_bytes"])
            ),
            failure_code=row["failure_code"],
            failure_detail=row["failure_detail"],
            index_snapshot_ids=tuple(int(value) for value in row["index_snapshot_ids"]),
            context_plan_fingerprint=row["context_plan_fingerprint"],
            duration_ms=int(row["duration_ms"]),
            started_at=row["started_at"],
        )
        for row in rows
    )
