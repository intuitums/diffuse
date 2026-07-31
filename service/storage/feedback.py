"""Durable, inspectable feedback signals for Diffuse review findings."""

from __future__ import annotations

import re
from dataclasses import dataclass

import psycopg2.extras

from service.models.feedback import (
    ReactionSyncResult,
    ReviewFeedbackSummary,
    ReviewReaction,
)
from service.scm import FeedbackSyncEvent, ReviewFeedbackCommentEvent


@dataclass(frozen=True)
class FeedbackSyncTarget:
    finding_thread_id: int
    repository_id: int
    pull_request_id: int
    finding_id: int
    root_comment_id: str
    generation: int
    finding_category: str
    finding_severity: str
    finding_security_classification: str | None = None
    scm_provider: str = "github"


def record_review_comment_feedback(
    conn,
    event: ReviewFeedbackCommentEvent,
    *,
    payload_sha256: str,
) -> str:
    """Record an authorized human reply only when it targets a Diffuse finding."""
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                repository.id AS repository_id,
                pull_request.id AS pull_request_id,
                thread.id AS finding_thread_id,
                finding.id AS finding_id,
                finding.file_path,
                finding.category,
                finding.severity,
                finding.security_classification
            FROM repositories AS repository
            JOIN pull_requests AS pull_request
              ON pull_request.repository_id = repository.id
             AND pull_request.number = %s
            JOIN finding_lineages AS lineage
              ON lineage.pull_request_id = pull_request.id
            JOIN finding_threads AS thread
              ON thread.lineage_id = lineage.id
             AND thread.scm_provider = %s
             AND thread.root_comment_id = %s
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
            WHERE repository.scm_provider = %s
              AND repository.scm_base_url = %s
              AND repository.full_name = %s
              AND repository.enabled = TRUE
            """,
            (
                event.number,
                event.provider,
                event.root_comment_id,
                event.provider,
                event.scm_base_url,
                event.repo_full_name,
            ),
        )
        target = cursor.fetchone()
        if not target or target["file_path"] != event.file_path:
            return "ignored:not_diffuse_thread"
        cursor.execute(
            """
            INSERT INTO review_feedback_events (
                repository_id,
                pull_request_id,
                finding_thread_id,
                finding_id,
                scm_provider,
                source_kind,
                signal_kind,
                event_action,
                event_key,
                source_external_id,
                source_comment_id,
                source_delivery_id,
                source_payload_sha256,
                actor_login,
                actor_authority,
                content,
                finding_category,
                finding_severity,
                finding_security_classification,
                source_created_at
            )
            VALUES (
                %s, %s, %s, %s, %s, 'reply', 'context', 'observed',
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (repository_id, event_key) DO NOTHING
            """,
            (
                int(target["repository_id"]),
                int(target["pull_request_id"]),
                int(target["finding_thread_id"]),
                int(target["finding_id"]),
                event.provider,
                event.event_key,
                event.external_comment_id,
                event.root_comment_id,
                event.delivery_id,
                payload_sha256,
                event.author,
                event.author_association,
                event.body,
                target["category"],
                target["severity"],
                target["security_classification"],
                event.created_at,
            ),
        )
        return "recorded" if cursor.rowcount == 1 else "duplicate"


def begin_feedback_sync(
    conn,
    *,
    workflow_job_id: int,
    event: FeedbackSyncEvent,
) -> FeedbackSyncTarget:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                state.finding_thread_id,
                state.generation,
                repository.id AS repository_id,
                pull_request.id AS pull_request_id,
                thread.root_comment_id,
                thread.scm_provider,
                finding.id AS finding_id,
                finding.category,
                finding.severity,
                finding.security_classification
            FROM workflow_jobs AS job
            JOIN review_feedback_sync_states AS state
              ON state.last_scheduled_job_id = job.id
            JOIN finding_threads AS thread
              ON thread.id = state.finding_thread_id
            JOIN finding_lineages AS lineage
              ON lineage.id = thread.lineage_id
            JOIN pull_requests AS pull_request
              ON pull_request.id = lineage.pull_request_id
            JOIN repositories AS repository
              ON repository.id = pull_request.repository_id
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
            WHERE job.id = %s
              AND job.job_type = 'sync_review_feedback'
              AND repository.enabled = TRUE
            FOR UPDATE OF state
            """,
            (workflow_job_id,),
        )
        row = cursor.fetchone()
        if (
            not row
            or row["root_comment_id"] != event.root_comment_id
            or row["scm_provider"] != event.provider
            or int(row["generation"]) != event.generation
        ):
            raise RuntimeError("Feedback sync job does not map to its finding thread")
        cursor.execute(
            """
            UPDATE review_feedback_sync_states
            SET last_started_at = now(),
                last_error_code = NULL,
                updated_at = now()
            WHERE finding_thread_id = %s
            """,
            (int(row["finding_thread_id"]),),
        )
    return FeedbackSyncTarget(
        finding_thread_id=int(row["finding_thread_id"]),
        repository_id=int(row["repository_id"]),
        pull_request_id=int(row["pull_request_id"]),
        finding_id=int(row["finding_id"]),
        root_comment_id=row["root_comment_id"],
        generation=int(row["generation"]),
        finding_category=row["category"],
        finding_severity=row["severity"],
        finding_security_classification=row["security_classification"],
        scm_provider=row["scm_provider"],
    )


def reconcile_review_reactions(
    conn,
    target: FeedbackSyncTarget,
    reactions: tuple[ReviewReaction, ...],
) -> ReactionSyncResult:
    reaction_ids = [reaction.external_id for reaction in reactions]
    if len(set(reaction_ids)) != len(reaction_ids):
        raise ValueError("Feedback sync returned duplicate reaction identifiers")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (source_external_id)
                source_external_id,
                signal_kind,
                event_action,
                actor_login,
                content,
                finding_id,
                finding_category,
                finding_severity,
                finding_security_classification
            FROM review_feedback_events
            WHERE finding_thread_id = %s
              AND source_kind = 'reaction'
            ORDER BY source_external_id, id DESC
            """,
            (target.finding_thread_id,),
        )
        previous = {
            row["source_external_id"]: row
            for row in cursor.fetchall()
            if row["event_action"] == "observed"
        }

        observed = 0
        for reaction in reactions:
            signal_kind = "positive" if reaction.content == "+1" else "negative"
            cursor.execute(
                """
                INSERT INTO review_feedback_events (
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    scm_provider,
                    source_kind,
                    signal_kind,
                    event_action,
                    event_key,
                    source_external_id,
                    source_comment_id,
                    actor_login,
                    actor_authority,
                    content,
                    finding_category,
                    finding_severity,
                    finding_security_classification,
                    source_created_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'reaction', %s, 'observed',
                    %s, %s, %s, %s, 'REPOSITORY_COLLABORATOR',
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (repository_id, event_key) DO NOTHING
                """,
                (
                    target.repository_id,
                    target.pull_request_id,
                    target.finding_thread_id,
                    target.finding_id,
                    target.scm_provider,
                    signal_kind,
                    f"reaction:{reaction.external_id}:observed",
                    reaction.external_id,
                    target.root_comment_id,
                    reaction.actor_login,
                    reaction.content,
                    target.finding_category,
                    target.finding_severity,
                    target.finding_security_classification,
                    reaction.created_at,
                ),
            )
            observed += cursor.rowcount

        current_ids = set(reaction_ids)
        withdrawn = 0
        for reaction_id, prior in previous.items():
            if reaction_id in current_ids:
                continue
            cursor.execute(
                """
                INSERT INTO review_feedback_events (
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    scm_provider,
                    source_kind,
                    signal_kind,
                    event_action,
                    event_key,
                    source_external_id,
                    source_comment_id,
                    actor_login,
                    actor_authority,
                    content,
                    finding_category,
                    finding_severity,
                    finding_security_classification
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'reaction', %s, 'withdrawn',
                    %s, %s, %s, %s, 'REPOSITORY_COLLABORATOR',
                    %s, %s, %s, %s
                )
                ON CONFLICT (repository_id, event_key) DO NOTHING
                """,
                (
                    target.repository_id,
                    target.pull_request_id,
                    target.finding_thread_id,
                    prior["finding_id"],
                    target.scm_provider,
                    prior["signal_kind"],
                    f"reaction:{reaction_id}:withdrawn",
                    reaction_id,
                    target.root_comment_id,
                    prior["actor_login"],
                    prior["content"],
                    prior["finding_category"],
                    prior["finding_severity"],
                    prior["finding_security_classification"],
                ),
            )
            withdrawn += cursor.rowcount

        cursor.execute(
            """
            UPDATE review_feedback_sync_states
            SET last_completed_at = now(),
                last_error_code = NULL,
                updated_at = now()
            WHERE finding_thread_id = %s
              AND generation = %s
            """,
            (target.finding_thread_id, target.generation),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Feedback sync generation changed before completion")

    return ReactionSyncResult(
        observed=observed,
        withdrawn=withdrawn,
        active_positive=sum(reaction.content == "+1" for reaction in reactions),
        active_negative=sum(reaction.content == "-1" for reaction in reactions),
    )


def mark_feedback_sync_failed(
    conn,
    *,
    workflow_job_id: int,
    error_code: str = "feedback_sync_failed",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Feedback sync error code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_feedback_sync_states AS state
            SET last_error_code = %s,
                updated_at = now()
            WHERE state.last_scheduled_job_id = %s
            """,
            (error_code, workflow_job_id),
        )


def record_review_outcomes(conn, review_run_id: int) -> None:
    """Project addressed/reopened lineage transitions into immutable memory signals."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                event.id AS lineage_event_id,
                event.transition,
                event.applied_at,
                repository.id AS repository_id,
                pull_request.id AS pull_request_id,
                thread.id AS finding_thread_id,
                thread.scm_provider,
                thread.root_comment_id,
                finding.id AS finding_id,
                finding.category,
                finding.severity,
                finding.security_classification
            FROM finding_lineage_events AS event
            JOIN finding_lineages AS lineage ON lineage.id = event.lineage_id
            JOIN pull_requests AS pull_request ON pull_request.id = lineage.pull_request_id
            JOIN repositories AS repository ON repository.id = pull_request.repository_id
            JOIN finding_threads AS thread ON thread.lineage_id = lineage.id
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
            WHERE event.review_run_id = %s
              AND event.transition IN ('addressed', 'reopened')
              AND event.applied_at IS NOT NULL
            ORDER BY event.id
            """,
            (review_run_id,),
        )
        for row in cursor.fetchall():
            cursor.execute(
                """
                INSERT INTO review_feedback_events (
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    scm_provider,
                    source_kind,
                    signal_kind,
                    event_action,
                    event_key,
                    source_external_id,
                    source_comment_id,
                    finding_category,
                    finding_severity,
                    finding_security_classification,
                    source_created_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'commit_outcome', %s, 'observed',
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (repository_id, event_key) DO NOTHING
                """,
                (
                    int(row["repository_id"]),
                    int(row["pull_request_id"]),
                    int(row["finding_thread_id"]),
                    int(row["finding_id"]),
                    row["scm_provider"],
                    row["transition"],
                    f"lineage-event:{row['lineage_event_id']}",
                    str(row["lineage_event_id"]),
                    row["root_comment_id"],
                    row["category"],
                    row["severity"],
                    row["security_classification"],
                    row["applied_at"],
                ),
            )


def load_repository_feedback_summary(
    conn,
    *,
    repository_id: int,
) -> tuple[ReviewFeedbackSummary, ...]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                thread.id AS finding_thread_id,
                thread.root_comment_id,
                finding.category,
                finding.severity,
                finding.security_classification,
                finding.file_path,
                COALESCE(reaction.positive_count, 0) AS positive_count,
                COALESCE(reaction.negative_count, 0) AS negative_count,
                COALESCE(other.context_count, 0) AS context_count,
                COALESCE(other.addressed_count, 0) AS addressed_count,
                COALESCE(other.reopened_count, 0) AS reopened_count,
                (
                    finding.category IN ('correctness', 'security')
                    OR finding.severity = 'critical'
                    OR COALESCE(other.has_protected_signal, FALSE)
                ) AS suppression_protected
            FROM finding_threads AS thread
            JOIN finding_lineages AS lineage ON lineage.id = thread.lineage_id
            JOIN pull_requests AS pull_request ON pull_request.id = lineage.pull_request_id
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
            LEFT JOIN LATERAL (
                SELECT
                    count(*) FILTER (WHERE current.signal_kind = 'positive')
                        AS positive_count,
                    count(*) FILTER (WHERE current.signal_kind = 'negative')
                        AS negative_count
                FROM (
                    SELECT DISTINCT ON (source_external_id)
                        source_external_id,
                        signal_kind,
                        event_action
                    FROM review_feedback_events
                    WHERE finding_thread_id = thread.id
                      AND source_kind = 'reaction'
                    ORDER BY source_external_id, id DESC
                ) AS current
                WHERE current.event_action = 'observed'
            ) AS reaction ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    count(*) FILTER (WHERE source_kind = 'reply')
                        AS context_count,
                    count(*) FILTER (WHERE signal_kind = 'addressed')
                        AS addressed_count,
                    count(*) FILTER (WHERE signal_kind = 'reopened')
                        AS reopened_count,
                    bool_or(suppression_protected) AS has_protected_signal
                FROM review_feedback_events
                WHERE finding_thread_id = thread.id
                  AND event_action = 'observed'
            ) AS other ON TRUE
            WHERE pull_request.repository_id = %s
            ORDER BY thread.id
            """,
            (repository_id,),
        )
        rows = cursor.fetchall()
    return tuple(
        ReviewFeedbackSummary(
            finding_thread_id=int(row["finding_thread_id"]),
            root_comment_id=row["root_comment_id"],
            category=row["category"],
            severity=row["severity"],
            file_path=row["file_path"],
            positive_reactions=int(row["positive_count"]),
            negative_reactions=int(row["negative_count"]),
            context_replies=int(row["context_count"]),
            addressed_outcomes=int(row["addressed_count"]),
            reopened_outcomes=int(row["reopened_count"]),
            suppression_protected=bool(row["suppression_protected"]),
            security_classification=row["security_classification"],
        )
        for row in rows
    )
