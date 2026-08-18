"""PostgreSQL persistence for finding lineage and SCM review threads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import psycopg2.extras
from diffuse_protocol.review import ReviewFinding

from diffuse.review.lineage import (
    FindingSnapshot,
    HistoricalFinding,
    ReviewContinuity,
    classify_finding_lineage,
)

ThreadOperationKind = Literal["address", "reopen"]
THREAD_OPERATION_KINDS = frozenset({"address", "reopen"})


@dataclass(frozen=True)
class PublishedFindingComment:
    fingerprint: str
    external_id: str
    external_node_id: str | None
    external_url: str | None
    thread_id: str | None = None


@dataclass(frozen=True)
class ThreadOperationHandle:
    id: int
    lineage_event_id: int
    kind: ThreadOperationKind
    idempotency_key: str
    root_comment_id: str
    root_comment_node_id: str | None
    thread_node_id: str | None


@dataclass(frozen=True)
class PublishedThreadOperation:
    external_reply_id: str
    external_reply_url: str | None
    thread_node_id: str


def _finding_from_row(row: dict) -> ReviewFinding:
    return ReviewFinding.model_validate(
        {
            "fingerprint": row["fingerprint"],
            "title": row["title"],
            "body": row["body"],
            "severity": row["severity"],
            "category": row["category"],
            "security_classification": row["security_classification"],
            "confidence": row["confidence"],
            "file_path": row["file_path"],
            "line": row["line"],
            "side": row["side"],
            "evidence": row["evidence"],
            "suggested_fix": row["suggested_fix"],
        }
    )


def _historical_findings(cursor, pull_request_id: int) -> tuple[HistoricalFinding, ...]:
    cursor.execute(
        """
        SELECT
            lineage.id AS lineage_id,
            lineage.status AS lineage_status,
            finding.fingerprint,
            finding.title,
            finding.body,
            finding.severity,
            finding.category,
            finding.security_classification,
            finding.confidence,
            finding.file_path,
            finding.line,
            finding.side,
            finding.evidence,
            finding.suggested_fix
        FROM finding_lineages AS lineage
        JOIN LATERAL (
            SELECT candidate.*
            FROM review_findings AS candidate
            JOIN finding_lineage_events AS occurrence
              ON occurrence.finding_id = candidate.id
             AND occurrence.applied_at IS NOT NULL
            WHERE candidate.lineage_id = lineage.id
            ORDER BY candidate.id DESC
            LIMIT 1
        ) AS finding ON TRUE
        WHERE lineage.pull_request_id = %s
          AND lineage.status IN ('active', 'addressed')
        ORDER BY lineage.id
        """,
        (pull_request_id,),
    )
    return tuple(
        HistoricalFinding(
            lineage_id=int(row["lineage_id"]),
            status=row["lineage_status"],
            finding=_finding_from_row(dict(row)),
        )
        for row in cursor.fetchall()
    )


def persist_finding_lineage(
    cursor,
    *,
    pull_request_id: int,
    review_run_id: int,
    findings: tuple[ReviewFinding, ...],
    touched_paths: frozenset[str],
    path_aliases: dict[str, str] | None = None,
) -> None:
    history = _historical_findings(cursor, pull_request_id)
    transitions = classify_finding_lineage(
        findings,
        history,
        touched_paths=touched_paths,
        path_aliases=path_aliases,
    )
    ordinals = {finding.fingerprint: index for index, finding in enumerate(findings)}

    for transition in transitions:
        lineage_id = transition.lineage_id
        finding_id: int | None = None
        if transition.finding is not None:
            finding = transition.finding
            if lineage_id is None:
                # A lineage whose publication could not anchor a root thread
                # stays `pending` across reviews, and history only offers
                # active or addressed lineages for matching. Reuse that row so
                # the retry keeps one durable identity per logical finding
                # instead of colliding on (pull_request_id, initial_fingerprint).
                cursor.execute(
                    """
                    INSERT INTO finding_lineages (
                        pull_request_id,
                        initial_fingerprint,
                        status,
                        first_seen_review_run_id,
                        last_seen_review_run_id
                    )
                    VALUES (%s, %s, 'pending', %s, NULL)
                    ON CONFLICT (pull_request_id, initial_fingerprint) DO UPDATE
                    SET updated_at = now()
                    WHERE finding_lineages.status = 'pending'
                    RETURNING id
                    """,
                    (
                        pull_request_id,
                        finding.fingerprint,
                        review_run_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError(
                        "Finding fingerprint collides with a published lineage"
                    )
                lineage_id = int(row["id"])
            cursor.execute(
                """
                INSERT INTO review_findings (
                    review_run_id,
                    lineage_id,
                    fingerprint,
                    ordinal,
                    title,
                    body,
                    severity,
                    category,
                    security_classification,
                    confidence,
                    file_path,
                    line,
                    side,
                    evidence,
                    suggested_fix
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    review_run_id,
                    lineage_id,
                    finding.fingerprint,
                    ordinals[finding.fingerprint],
                    finding.title,
                    finding.body,
                    finding.severity.value,
                    finding.category.value,
                    finding.security_classification.value
                    if finding.security_classification is not None
                    else None,
                    finding.confidence,
                    finding.file_path,
                    finding.line,
                    finding.side,
                    finding.evidence,
                    finding.suggested_fix,
                ),
            )
            finding_id = int(cursor.fetchone()["id"])

        cursor.execute(
            """
            INSERT INTO finding_lineage_events (
                lineage_id,
                review_run_id,
                finding_id,
                transition
            )
            VALUES (%s, %s, %s, %s)
            """,
            (lineage_id, review_run_id, finding_id, transition.kind),
        )


def activate_finding_lineage_events(
    conn,
    review_run_id: int,
    *,
    unanchored_fingerprints: frozenset[str] = frozenset(),
) -> None:
    """Apply one published review's pending lineage transitions atomically.

    ``unanchored_fingerprints`` holds the `new` findings the publication tried
    and failed to attach to an inline root thread. Activating those lineages
    would strand them: an `active` lineage without a ``finding_threads`` row can
    never be resolved, reopened, targeted by an `@diffuse` reply, or scheduled
    for feedback sync, and later reviews classify it as `persistent` so the
    inline attach is never retried. Their transitions stay provisional instead,
    so the next review re-derives them as `new` and re-attempts attachment.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                event.id,
                event.lineage_id,
                event.transition,
                lineage.status,
                finding.fingerprint,
                EXISTS (
                    SELECT 1
                    FROM finding_threads AS thread
                    WHERE thread.lineage_id = event.lineage_id
                ) AS has_thread
            FROM finding_lineage_events AS event
            JOIN finding_lineages AS lineage ON lineage.id = event.lineage_id
            LEFT JOIN review_findings AS finding ON finding.id = event.finding_id
            WHERE event.review_run_id = %s
              AND event.applied_at IS NULL
            ORDER BY event.id
            FOR UPDATE OF event, lineage
            """,
            (review_run_id,),
        )
        events = cursor.fetchall()
        for event in events:
            transition = event["transition"]
            if (
                transition == "new"
                and not event["has_thread"]
                and event["fingerprint"] in unanchored_fingerprints
            ):
                continue
            expected_status = {
                "new": "pending",
                "persistent": "active",
                "reopened": "addressed",
                "addressed": "active",
            }[transition]
            if event["status"] != expected_status:
                raise RuntimeError(
                    "Finding lineage changed before its review was published"
                )
            if transition == "addressed":
                cursor.execute(
                    """
                    UPDATE finding_lineages
                    SET status = 'addressed',
                        addressed_review_run_id = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (review_run_id, int(event["lineage_id"])),
                )
            else:
                cursor.execute(
                    """
                    UPDATE finding_lineages
                    SET status = 'active',
                        last_seen_review_run_id = %s,
                        addressed_review_run_id = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (review_run_id, int(event["lineage_id"])),
                )
            cursor.execute(
                """
                UPDATE finding_lineage_events
                SET applied_at = now()
                WHERE id = %s
                """,
                (int(event["id"]),),
            )


def discard_unpublished_finding_lineage(conn, review_run_id: int) -> None:
    """Remove lineage projections from a review that will never be published."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM review_runs WHERE id = %s FOR UPDATE",
            (review_run_id,),
        )
        review = cursor.fetchone()
        if not review or review[0] in {"published", "skipped"}:
            return
        cursor.execute(
            """
            DELETE FROM finding_lineage_events
            WHERE review_run_id = %s
              AND applied_at IS NULL
            """,
            (review_run_id,),
        )
        cursor.execute(
            "DELETE FROM review_findings WHERE review_run_id = %s",
            (review_run_id,),
        )
        cursor.execute(
            """
            DELETE FROM finding_lineages
            WHERE first_seen_review_run_id = %s
              AND status = 'pending'
            """,
            (review_run_id,),
        )


def latest_published_review_head(
    conn,
    *,
    pull_request_id: int,
    before_review_run_id: int,
) -> str | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT head_sha
            FROM review_runs
            WHERE pull_request_id = %s
              AND id <> %s
              AND status = 'published'
            ORDER BY published_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (pull_request_id, before_review_run_id),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def load_review_continuity(conn, review_run_id: int) -> ReviewContinuity:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                event.transition,
                finding.fingerprint,
                lineage.id AS lineage_id,
                historical.title,
                historical.body,
                historical.severity,
                historical.category,
                historical.security_classification,
                historical.confidence,
                historical.file_path,
                historical.line,
                historical.side,
                historical.evidence,
                historical.suggested_fix
            FROM finding_lineage_events AS event
            JOIN finding_lineages AS lineage ON lineage.id = event.lineage_id
            LEFT JOIN review_findings AS finding ON finding.id = event.finding_id
            JOIN LATERAL (
                SELECT previous.*
                FROM review_findings AS previous
                WHERE previous.lineage_id = lineage.id
                ORDER BY previous.id DESC
                LIMIT 1
            ) AS historical ON TRUE
            WHERE event.review_run_id = %s
            ORDER BY event.id
            """,
            (review_run_id,),
        )
        events = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                lineage.id AS lineage_id,
                finding.fingerprint,
                finding.title,
                finding.body,
                finding.severity,
                finding.category,
                finding.security_classification,
                finding.confidence,
                finding.file_path,
                finding.line,
                finding.side,
                finding.evidence,
                finding.suggested_fix
            FROM finding_lineages AS lineage
            JOIN LATERAL (
                SELECT candidate.*
                FROM review_findings AS candidate
                JOIN finding_lineage_events AS occurrence
                  ON occurrence.finding_id = candidate.id
                 AND occurrence.applied_at IS NOT NULL
                WHERE candidate.lineage_id = lineage.id
                ORDER BY candidate.id DESC
                LIMIT 1
            ) AS finding ON TRUE
            WHERE lineage.pull_request_id = (
                SELECT pull_request_id FROM review_runs WHERE id = %s
            )
              AND lineage.status = 'active'
            ORDER BY
                CASE finding.severity
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    ELSE 3
                END,
                finding.file_path,
                finding.line,
                lineage.id
            """,
            (review_run_id,),
        )
        active_rows = cursor.fetchall()

    open_by_lineage = {
        int(row["lineage_id"]): _finding_from_row(dict(row))
        for row in active_rows
    }
    for row in events:
        lineage_id = int(row["lineage_id"])
        if row["transition"] == "addressed":
            open_by_lineage.pop(lineage_id, None)
        else:
            open_by_lineage[lineage_id] = _finding_from_row(dict(row))
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    open_findings = tuple(
        finding
        for _lineage_id, finding in sorted(
            open_by_lineage.items(),
            key=lambda item: (
                severity_order[item[1].severity.value],
                item[1].file_path,
                item[1].line,
                item[0],
            ),
        )
    )

    def fingerprints(kind: str) -> tuple[str, ...]:
        return tuple(
            row["fingerprint"]
            for row in events
            if row["transition"] == kind and row["fingerprint"] is not None
        )

    return ReviewContinuity(
        new_fingerprints=fingerprints("new"),
        persistent_fingerprints=fingerprints("persistent"),
        reopened_fingerprints=fingerprints("reopened"),
        addressed=tuple(
            FindingSnapshot(
                lineage_id=int(row["lineage_id"]),
                title=row["title"],
                severity=row["severity"],
                category=row["category"],
                file_path=row["file_path"],
                line=int(row["line"]),
                side=row["side"],
            )
            for row in events
            if row["transition"] == "addressed"
        ),
        open_findings=open_findings,
    )


def record_finding_threads(
    conn,
    *,
    review_run_id: int,
    scm_provider: str,
    comments: tuple[PublishedFindingComment, ...],
) -> None:
    if not comments:
        return
    if scm_provider != "github":
        raise ValueError("Finding threads require a supported SCM provider")
    with conn.cursor() as cursor:
        for comment in comments:
            cursor.execute(
                """
                SELECT finding.lineage_id
                FROM review_findings AS finding
                JOIN finding_lineage_events AS event
                  ON event.finding_id = finding.id
                WHERE finding.review_run_id = %s
                  AND finding.fingerprint = %s
                  AND event.transition = 'new'
                """,
                (review_run_id, comment.fingerprint),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("Published comment does not map to a new finding")
            lineage_id = int(row[0])
            cursor.execute(
                """
                INSERT INTO finding_threads (
                    lineage_id,
                    scm_provider,
                    root_comment_id,
                    root_comment_node_id,
                    thread_node_id,
                    status,
                    created_review_run_id
                )
                VALUES (%s, %s, %s, %s, %s, 'active', %s)
                ON CONFLICT (lineage_id) DO UPDATE
                SET root_comment_node_id = COALESCE(
                        finding_threads.root_comment_node_id,
                        EXCLUDED.root_comment_node_id
                    ),
                    thread_node_id = COALESCE(
                        finding_threads.thread_node_id,
                        EXCLUDED.thread_node_id
                    ),
                    updated_at = now()
                WHERE finding_threads.root_comment_id = EXCLUDED.root_comment_id
                """,
                (
                    lineage_id,
                    scm_provider,
                    comment.external_id,
                    comment.external_node_id,
                    comment.thread_id,
                    review_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Finding thread conflicts with its published root")
            cursor.execute(
                """
                INSERT INTO review_feedback_sync_states (finding_thread_id)
                SELECT id
                FROM finding_threads
                WHERE lineage_id = %s
                ON CONFLICT (finding_thread_id) DO NOTHING
                """,
                (lineage_id,),
            )


def begin_thread_operations(
    conn,
    *,
    review_run_id: int,
) -> tuple[ThreadOperationHandle, ...]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO finding_thread_operations (
                lineage_event_id,
                finding_thread_id,
                operation_kind,
                idempotency_key,
                status
            )
            SELECT
                event.id,
                thread.id,
                CASE event.transition
                    WHEN 'addressed' THEN 'address'
                    ELSE 'reopen'
                END,
                'finding-lineage-event:' || event.id || ':thread-state',
                'pending'
            FROM finding_lineage_events AS event
            JOIN finding_threads AS thread ON thread.lineage_id = event.lineage_id
            WHERE event.review_run_id = %s
              AND event.transition IN ('addressed', 'reopened')
              AND event.applied_at IS NOT NULL
            ON CONFLICT (lineage_event_id) DO NOTHING
            """,
            (review_run_id,),
        )
        cursor.execute(
            """
            SELECT
                operation.id,
                operation.lineage_event_id,
                operation.operation_kind,
                operation.idempotency_key,
                operation.status,
                thread.root_comment_id,
                thread.root_comment_node_id,
                thread.thread_node_id
            FROM finding_thread_operations AS operation
            JOIN finding_threads AS thread ON thread.id = operation.finding_thread_id
            JOIN finding_lineage_events AS event
              ON event.id = operation.lineage_event_id
            WHERE event.review_run_id = %s
            ORDER BY operation.id
            FOR UPDATE OF operation
            """,
            (review_run_id,),
        )
        rows = cursor.fetchall()
        handles: list[ThreadOperationHandle] = []
        for row in rows:
            if row["status"] == "published":
                continue
            cursor.execute(
                """
                UPDATE finding_thread_operations
                SET status = 'publishing',
                    attempt_count = attempt_count + 1,
                    error_code = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (int(row["id"]),),
            )
            handles.append(
                ThreadOperationHandle(
                    id=int(row["id"]),
                    lineage_event_id=int(row["lineage_event_id"]),
                    kind=row["operation_kind"],
                    idempotency_key=row["idempotency_key"],
                    root_comment_id=row["root_comment_id"],
                    root_comment_node_id=row["root_comment_node_id"],
                    thread_node_id=row["thread_node_id"],
                )
            )
    return tuple(handles)


def mark_thread_operation_published(
    conn,
    operation_id: int,
    *,
    result: PublishedThreadOperation,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE finding_thread_operations
            SET status = 'published',
                external_reply_id = %s,
                external_reply_url = %s,
                error_code = NULL,
                published_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status IN ('publishing', 'failed')
            RETURNING finding_thread_id, operation_kind, lineage_event_id
            """,
            (
                result.external_reply_id,
                result.external_reply_url,
                operation_id,
            ),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Finding-thread operation is not publishable")
        thread_id, operation_kind, lineage_event_id = row
        cursor.execute(
            """
            UPDATE finding_threads
            SET status = %s,
                thread_node_id = %s,
                last_synced_event_id = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                "addressed" if operation_kind == "address" else "active",
                result.thread_node_id,
                lineage_event_id,
                thread_id,
            ),
        )


def mark_thread_operation_failed(
    conn,
    operation_id: int,
    *,
    error_code: str = "finding_thread_publication_failed",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Finding-thread error code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE finding_thread_operations
            SET status = 'failed',
                error_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status <> 'published'
            """,
            (error_code, operation_id),
        )
