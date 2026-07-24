"""PostgreSQL-backed workflow queue and webhook idempotency."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg2.extras

from service.scm import (
    FeedbackSyncEvent,
    PullRequestEvent,
    PushEvent,
    ReviewConversationEvent,
)

ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


class RepositoryNotOnboardedError(LookupError):
    pass


class DeliveryConflictError(RuntimeError):
    pass


class EventOrderConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnqueueResult:
    job_id: int | None
    state: str

    @property
    def accepted(self) -> bool:
        return self.state == "queued"


@dataclass(frozen=True)
class WorkflowJob:
    id: int
    repository_id: int
    pull_request_id: int | None
    job_type: str
    scope_key: str
    base_revision: str
    revision: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int


def _advisory_lock_id(identity: str) -> int:
    return int.from_bytes(
        hashlib.sha256(identity.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _validated_error_code(error_code: str) -> str:
    if not ERROR_CODE_PATTERN.fullmatch(error_code):
        raise ValueError("error_code must contain only lowercase letters, digits, and underscores")
    return error_code


def _record_pull_request_lifecycle(
    cursor,
    *,
    pull_request_id: int,
    delivery_id: int,
    event: PullRequestEvent,
) -> None:
    cursor.execute(
        """
        INSERT INTO pull_request_lifecycle_events (
            pull_request_id,
            scm_webhook_delivery_id,
            action,
            state,
            source_event_at,
            source_created_at,
            source_closed_at,
            source_merged_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            pull_request_id,
            delivery_id,
            event.action,
            event.state,
            event.lifecycle_at,
            event.source_created_at or None,
            event.source_closed_at or None,
            event.source_merged_at or None,
        ),
    )


def enqueue_review_event(
    conn,
    event: PullRequestEvent,
    *,
    payload_sha256: str,
) -> EnqueueResult:
    """Record a delivery and enqueue at most one review for a PR revision."""
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_advisory_lock_id(event.scope_key),),
        )
        cursor.execute(
            """
            SELECT id
            FROM repositories
            WHERE scm_provider = %s
              AND scm_base_url = %s
              AND full_name = %s
              AND enabled = TRUE
            """,
            (event.provider, event.scm_base_url, event.repo_full_name),
        )
        repository = cursor.fetchone()
        if not repository:
            raise RepositoryNotOnboardedError(event.repo_full_name)
        repository_id = int(repository[0])

        cursor.execute(
            """
            INSERT INTO scm_webhook_deliveries (
                scm_provider,
                scm_base_url,
                delivery_id,
                event_name,
                payload_sha256
            )
            VALUES (%s, %s, %s, 'pull_request', %s)
            ON CONFLICT (scm_provider, scm_base_url, delivery_id) DO NOTHING
            RETURNING id
            """,
            (
                event.provider,
                event.scm_base_url,
                event.delivery_id,
                payload_sha256,
            ),
        )
        delivery = cursor.fetchone()
        if not delivery:
            cursor.execute(
                """
                SELECT
                    delivery.payload_sha256,
                    delivery.workflow_job_id,
                    job.status
                FROM scm_webhook_deliveries AS delivery
                LEFT JOIN workflow_jobs AS job ON job.id = delivery.workflow_job_id
                WHERE delivery.scm_provider = %s
                  AND delivery.scm_base_url = %s
                  AND delivery.delivery_id = %s
                """,
                (event.provider, event.scm_base_url, event.delivery_id),
            )
            existing_hash, job_id, job_status = cursor.fetchone()
            if existing_hash != payload_sha256:
                raise DeliveryConflictError(event.delivery_id)
            return EnqueueResult(
                job_id=int(job_id) if job_id is not None else None,
                state=f"duplicate_delivery:{job_status or 'recorded'}",
            )
        delivery_id = int(delivery[0])

        cursor.execute(
            """
            SELECT id, head_sha, latest_event_at
            FROM pull_requests
            WHERE repository_id = %s
              AND number = %s
            FOR UPDATE
            """,
            (repository_id, event.number),
        )
        pull_request = cursor.fetchone()
        event_time = datetime.fromisoformat(event.updated_at)
        if pull_request:
            pull_request_id = int(pull_request[0])
            current_head_sha = pull_request[1]
            current_event_time = pull_request[2]
            if event_time < current_event_time:
                _record_pull_request_lifecycle(
                    cursor,
                    pull_request_id=pull_request_id,
                    delivery_id=delivery_id,
                    event=event,
                )
                if event.source_created_at:
                    cursor.execute(
                        """
                        UPDATE pull_requests
                        SET source_created_at = COALESCE(source_created_at, %s)
                        WHERE id = %s
                        """,
                        (event.source_created_at, pull_request_id),
                    )
                return EnqueueResult(job_id=None, state="stale_delivery")
            if event_time == current_event_time and event.head_sha != current_head_sha:
                raise EventOrderConflictError(event.scope_key)
            cursor.execute(
                """
                UPDATE pull_requests
                SET web_url = %s,
                    base_sha = %s,
                    head_sha = %s,
                    author = %s,
                    base_branch = %s,
                    head_branch = %s,
                    is_draft = %s,
                    labels = %s,
                    title = %s,
                    description = %s,
                    state = %s,
                    changed_file_count = %s,
                    additions = %s,
                    deletions = %s,
                    source_created_at = COALESCE(%s, source_created_at),
                    source_closed_at = %s,
                    source_merged_at = %s,
                    latest_event_at = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    event.web_url,
                    event.base_sha,
                    event.head_sha,
                    event.author,
                    event.base_branch,
                    event.head_branch,
                    event.is_draft,
                    list(event.labels),
                    event.title,
                    event.description,
                    event.state,
                    event.changed_file_count,
                    event.additions,
                    event.deletions,
                    event.source_created_at or None,
                    event.source_closed_at or None,
                    event.source_merged_at or None,
                    event.updated_at,
                    pull_request_id,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO pull_requests (
                    repository_id,
                    number,
                    web_url,
                    base_sha,
                    head_sha,
                    author,
                    base_branch,
                    head_branch,
                    is_draft,
                    labels,
                    title,
                    description,
                    state,
                    changed_file_count,
                    additions,
                    deletions,
                    source_created_at,
                    source_closed_at,
                    source_merged_at,
                    latest_event_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    repository_id,
                    event.number,
                    event.web_url,
                    event.base_sha,
                    event.head_sha,
                    event.author,
                    event.base_branch,
                    event.head_branch,
                    event.is_draft,
                    list(event.labels),
                    event.title,
                    event.description,
                    event.state,
                    event.changed_file_count,
                    event.additions,
                    event.deletions,
                    event.source_created_at or None,
                    event.source_closed_at or None,
                    event.source_merged_at or None,
                    event.updated_at,
                ),
            )
            pull_request_id = int(cursor.fetchone()[0])

        _record_pull_request_lifecycle(
            cursor,
            pull_request_id=pull_request_id,
            delivery_id=delivery_id,
            event=event,
        )

        if event.state != "open":
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET status = 'cancelled',
                    completed_at = now(),
                    updated_at = now()
                WHERE scope_key = %s
                  AND status = 'queued'
                """,
                (event.scope_key,),
            )
            return EnqueueResult(
                job_id=None,
                state=f"pull_request_{event.state}",
            )

        cursor.execute(
            """
            SELECT id, status
            FROM workflow_jobs
            WHERE idempotency_key = %s
            """,
            (event.idempotency_key,),
        )
        existing_job = cursor.fetchone()
        if existing_job:
            job_id = int(existing_job[0])
            cursor.execute(
                """
                UPDATE scm_webhook_deliveries
                SET workflow_job_id = %s
                WHERE id = %s
                """,
                (job_id, delivery_id),
            )
            return EnqueueResult(
                job_id=job_id,
                state=f"duplicate_revision:{existing_job[1]}",
            )

        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'superseded',
                completed_at = now(),
                updated_at = now()
            WHERE scope_key = %s
              AND status = 'queued'
            """,
            (event.scope_key,),
        )
        cursor.execute(
            """
            INSERT INTO workflow_jobs (
                repository_id,
                pull_request_id,
                job_type,
                idempotency_key,
                scope_key,
                base_revision,
                revision,
                payload
            )
            VALUES (%s, %s, 'review_pull_request', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                repository_id,
                pull_request_id,
                event.idempotency_key,
                event.scope_key,
                event.base_sha,
                event.head_sha,
                psycopg2.extras.Json(event.to_payload()),
            ),
        )
        job_id = int(cursor.fetchone()[0])
        cursor.execute(
            """
            UPDATE scm_webhook_deliveries
            SET workflow_job_id = %s
            WHERE id = %s
            """,
            (job_id, delivery_id),
        )
        return EnqueueResult(job_id=job_id, state="queued")


def enqueue_review_conversation_event(
    conn,
    event: ReviewConversationEvent,
    *,
    payload_sha256: str,
) -> EnqueueResult:
    """Queue one authorized question only when it targets a Diffuse-owned thread."""
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_advisory_lock_id(event.scope_key),),
        )
        cursor.execute(
            """
            SELECT id
            FROM repositories
            WHERE scm_provider = %s
              AND scm_base_url = %s
              AND full_name = %s
              AND enabled = TRUE
            """,
            (event.provider, event.scm_base_url, event.repo_full_name),
        )
        repository = cursor.fetchone()
        if not repository:
            raise RepositoryNotOnboardedError(event.repo_full_name)
        repository_id = int(repository[0])

        cursor.execute(
            """
            INSERT INTO scm_webhook_deliveries (
                scm_provider,
                scm_base_url,
                delivery_id,
                event_name,
                payload_sha256
            )
            VALUES (%s, %s, %s, 'pull_request_review_comment', %s)
            ON CONFLICT (scm_provider, scm_base_url, delivery_id) DO NOTHING
            RETURNING id
            """,
            (
                event.provider,
                event.scm_base_url,
                event.delivery_id,
                payload_sha256,
            ),
        )
        delivery = cursor.fetchone()
        if not delivery:
            cursor.execute(
                """
                SELECT
                    delivery.payload_sha256,
                    delivery.workflow_job_id,
                    job.status
                FROM scm_webhook_deliveries AS delivery
                LEFT JOIN workflow_jobs AS job ON job.id = delivery.workflow_job_id
                WHERE delivery.scm_provider = %s
                  AND delivery.scm_base_url = %s
                  AND delivery.delivery_id = %s
                """,
                (event.provider, event.scm_base_url, event.delivery_id),
            )
            existing = cursor.fetchone()
            if not existing or existing[0] != payload_sha256:
                raise DeliveryConflictError(event.delivery_id)
            job_id = int(existing[1]) if existing[1] is not None else None
            return EnqueueResult(
                job_id=job_id,
                state=f"duplicate_delivery:{existing[2] or 'recorded'}",
            )
        delivery_id = int(delivery[0])

        cursor.execute(
            """
            SELECT
                pull_request.id,
                thread.id,
                finding.file_path
            FROM pull_requests AS pull_request
            JOIN finding_lineages AS lineage
              ON lineage.pull_request_id = pull_request.id
            JOIN finding_threads AS thread
              ON thread.lineage_id = lineage.id
             AND thread.scm_provider = %s
             AND thread.root_comment_id = %s
            JOIN LATERAL (
                SELECT candidate.file_path
                FROM review_findings AS candidate
                JOIN finding_lineage_events AS occurrence
                  ON occurrence.finding_id = candidate.id
                 AND occurrence.applied_at IS NOT NULL
                WHERE candidate.lineage_id = lineage.id
                ORDER BY candidate.id DESC
                LIMIT 1
            ) AS finding ON TRUE
            WHERE pull_request.repository_id = %s
              AND pull_request.number = %s
            """,
            (
                event.provider,
                event.root_comment_id,
                repository_id,
                event.number,
            ),
        )
        target = cursor.fetchone()
        if not target or target[2] != event.file_path:
            return EnqueueResult(job_id=None, state="ignored:not_diffuse_thread")
        pull_request_id = int(target[0])
        finding_thread_id = int(target[1])

        cursor.execute(
            """
            SELECT id, status
            FROM workflow_jobs
            WHERE idempotency_key = %s
            """,
            (event.idempotency_key,),
        )
        existing_job = cursor.fetchone()
        if existing_job:
            job_id = int(existing_job[0])
            cursor.execute(
                """
                UPDATE scm_webhook_deliveries
                SET workflow_job_id = %s
                WHERE id = %s
                """,
                (job_id, delivery_id),
            )
            return EnqueueResult(
                job_id=job_id,
                state=f"duplicate_comment:{existing_job[1]}",
            )

        cursor.execute(
            """
            INSERT INTO workflow_jobs (
                repository_id,
                pull_request_id,
                job_type,
                idempotency_key,
                scope_key,
                base_revision,
                revision,
                payload
            )
            VALUES (%s, %s, 'answer_review_comment', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                repository_id,
                pull_request_id,
                event.idempotency_key,
                event.scope_key,
                event.base_sha,
                event.head_sha,
                psycopg2.extras.Json(event.to_payload()),
            ),
        )
        job_id = int(cursor.fetchone()[0])
        cursor.execute(
            """
            INSERT INTO review_conversation_messages (
                workflow_job_id,
                pull_request_id,
                finding_thread_id,
                scm_provider,
                external_comment_id,
                root_comment_id,
                author_login,
                author_association,
                question,
                file_path,
                line,
                side,
                diff_hunk,
                comment_commit_sha,
                base_sha,
                head_sha,
                source_created_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                job_id,
                pull_request_id,
                finding_thread_id,
                event.provider,
                event.external_comment_id,
                event.root_comment_id,
                event.author,
                event.author_association,
                event.question,
                event.file_path,
                event.line,
                event.side,
                event.diff_hunk,
                event.comment_commit_sha,
                event.base_sha,
                event.head_sha,
                event.created_at,
            ),
        )
        cursor.execute(
            """
            UPDATE scm_webhook_deliveries
            SET workflow_job_id = %s
            WHERE id = %s
            """,
            (job_id, delivery_id),
        )
        return EnqueueResult(job_id=job_id, state="queued")


def schedule_due_feedback_sync_jobs(
    conn,
    *,
    api_base_url: str,
    gitlab_scm_base_url: str = "https://gitlab.com",
    gitlab_api_base_url: str = "https://gitlab.com/api/v4",
    interval_seconds: int = 900,
    limit: int = 20,
) -> int:
    """Durably schedule due SCM reaction reconciliation without duplicate work."""
    if not 60 <= interval_seconds <= 86_400:
        raise ValueError("Feedback sync interval must be between 60 and 86400 seconds")
    if not 1 <= limit <= 100:
        raise ValueError("Feedback sync batch limit must be between 1 and 100")

    scheduled = 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO review_feedback_sync_states (finding_thread_id)
            SELECT id
            FROM finding_threads
            ON CONFLICT (finding_thread_id) DO NOTHING
            """
        )
        cursor.execute(
            """
            SELECT
                state.finding_thread_id,
                state.generation,
                repository.scm_provider,
                repository.scm_base_url,
                repository.full_name,
                pull_request.id AS pull_request_id,
                pull_request.number,
                pull_request.base_sha,
                pull_request.head_sha,
                thread.root_comment_id
            FROM review_feedback_sync_states AS state
            JOIN finding_threads AS thread
              ON thread.id = state.finding_thread_id
            JOIN finding_lineages AS lineage
              ON lineage.id = thread.lineage_id
            JOIN pull_requests AS pull_request
              ON pull_request.id = lineage.pull_request_id
            JOIN repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE state.next_sync_at <= now()
              AND repository.enabled = TRUE
            ORDER BY state.next_sync_at, state.finding_thread_id
            FOR UPDATE OF state SKIP LOCKED
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        for row in rows:
            provider_api_base_url = (
                api_base_url
                if row["scm_provider"] == "github"
                else gitlab_api_base_url
                if row["scm_base_url"] == gitlab_scm_base_url
                else f"{row['scm_base_url']}/api/v4"
            )
            event = FeedbackSyncEvent(
                provider=row["scm_provider"],
                scm_base_url=row["scm_base_url"],
                api_base_url=provider_api_base_url,
                repo_full_name=row["full_name"],
                number=int(row["number"]),
                root_comment_id=row["root_comment_id"],
                generation=int(row["generation"]) + 1,
                base_sha=row["base_sha"],
                head_sha=row["head_sha"],
            )
            cursor.execute(
                """
                SELECT 1
                FROM workflow_jobs
                WHERE scope_key = %s
                  AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (event.scope_key,),
            )
            if cursor.fetchone():
                continue
            cursor.execute(
                """
                INSERT INTO workflow_jobs (
                    repository_id,
                    pull_request_id,
                    job_type,
                    idempotency_key,
                    scope_key,
                    base_revision,
                    revision,
                    payload,
                    priority
                )
                SELECT
                    repository_id,
                    %s,
                    'sync_review_feedback',
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    -10
                FROM pull_requests
                WHERE id = %s
                RETURNING id
                """,
                (
                    int(row["pull_request_id"]),
                    event.idempotency_key,
                    event.scope_key,
                    event.base_sha,
                    event.head_sha,
                    psycopg2.extras.Json(event.to_payload()),
                    int(row["pull_request_id"]),
                ),
            )
            job_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                UPDATE review_feedback_sync_states
                SET generation = %s,
                    next_sync_at = now() + (%s * interval '1 second'),
                    last_scheduled_job_id = %s,
                    updated_at = now()
                WHERE finding_thread_id = %s
                """,
                (
                    event.generation,
                    interval_seconds,
                    job_id,
                    int(row["finding_thread_id"]),
                ),
            )
            scheduled += 1
    return scheduled


def enqueue_repository_index_event(
    conn,
    event: PushEvent,
    *,
    payload_sha256: str,
) -> EnqueueResult:
    """Record a default-branch push and enqueue its exact commit once."""
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_advisory_lock_id(event.scope_key),),
        )
        cursor.execute(
            """
            SELECT id, default_branch, enabled, clone_url
            FROM repositories
            WHERE scm_provider = %s
              AND scm_base_url = %s
              AND full_name = %s
            """,
            (event.provider, event.scm_base_url, event.repo_full_name),
        )
        repository = cursor.fetchone()
        if (
            not repository
            or not repository[2]
            or not repository[3]
            or repository[1] != event.default_branch
        ):
            raise RepositoryNotOnboardedError(event.repo_full_name)
        repository_id = int(repository[0])

        cursor.execute(
            """
            INSERT INTO scm_webhook_deliveries (
                scm_provider,
                scm_base_url,
                delivery_id,
                event_name,
                payload_sha256
            )
            VALUES (%s, %s, %s, 'push', %s)
            ON CONFLICT (scm_provider, scm_base_url, delivery_id) DO NOTHING
            RETURNING id
            """,
            (
                event.provider,
                event.scm_base_url,
                event.delivery_id,
                payload_sha256,
            ),
        )
        delivery = cursor.fetchone()
        if not delivery:
            cursor.execute(
                """
                SELECT
                    delivery.payload_sha256,
                    delivery.workflow_job_id,
                    job.status
                FROM scm_webhook_deliveries AS delivery
                LEFT JOIN workflow_jobs AS job ON job.id = delivery.workflow_job_id
                WHERE delivery.scm_provider = %s
                  AND delivery.scm_base_url = %s
                  AND delivery.delivery_id = %s
                """,
                (event.provider, event.scm_base_url, event.delivery_id),
            )
            existing_hash, job_id, job_status = cursor.fetchone()
            if existing_hash != payload_sha256:
                raise DeliveryConflictError(event.delivery_id)
            return EnqueueResult(
                job_id=int(job_id) if job_id is not None else None,
                state=f"duplicate_delivery:{job_status or 'recorded'}",
            )
        delivery_id = int(delivery[0])

        cursor.execute(
            """
            SELECT id, commit_sha, latest_event_at
            FROM repository_refs
            WHERE repository_id = %s
              AND ref_name = %s
            FOR UPDATE
            """,
            (repository_id, event.ref_name),
        )
        repository_ref = cursor.fetchone()
        event_time = datetime.fromisoformat(event.pushed_at)
        if repository_ref:
            repository_ref_id = int(repository_ref[0])
            if event_time < repository_ref[2]:
                return EnqueueResult(job_id=None, state="stale_delivery")
            if event_time == repository_ref[2] and event.after_sha != repository_ref[1]:
                raise EventOrderConflictError(event.scope_key)
            cursor.execute(
                """
                UPDATE repository_refs
                SET commit_sha = %s,
                    latest_event_at = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (event.after_sha, event.pushed_at, repository_ref_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO repository_refs (
                    repository_id,
                    ref_name,
                    commit_sha,
                    latest_event_at
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    repository_id,
                    event.ref_name,
                    event.after_sha,
                    event.pushed_at,
                ),
            )

        cursor.execute(
            """
            SELECT id, status
            FROM workflow_jobs
            WHERE idempotency_key = %s
            """,
            (event.idempotency_key,),
        )
        existing_job = cursor.fetchone()
        if existing_job:
            job_id = int(existing_job[0])
            cursor.execute(
                """
                UPDATE scm_webhook_deliveries
                SET workflow_job_id = %s
                WHERE id = %s
                """,
                (job_id, delivery_id),
            )
            return EnqueueResult(
                job_id=job_id,
                state=f"duplicate_revision:{existing_job[1]}",
            )

        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'superseded',
                completed_at = now(),
                updated_at = now()
            WHERE scope_key = %s
              AND status = 'queued'
              AND revision <> %s
            """,
            (event.scope_key, event.after_sha),
        )
        cursor.execute(
            """
            INSERT INTO workflow_jobs (
                repository_id,
                pull_request_id,
                job_type,
                idempotency_key,
                scope_key,
                base_revision,
                revision,
                payload
            )
            VALUES (%s, NULL, 'index_repository', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                repository_id,
                event.idempotency_key,
                event.scope_key,
                event.after_sha,
                event.after_sha,
                psycopg2.extras.Json(event.to_payload()),
            ),
        )
        job_id = int(cursor.fetchone()[0])
        cursor.execute(
            """
            UPDATE scm_webhook_deliveries
            SET workflow_job_id = %s
            WHERE id = %s
            """,
            (job_id, delivery_id),
        )
        return EnqueueResult(job_id=job_id, state="queued")


def claim_workflow_job(
    conn,
    worker_id: str,
    *,
    lease_seconds: int = 900,
) -> WorkflowJob | None:
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE workflow_attempts AS attempt
            SET status = 'lease_expired',
                error_code = 'lease_expired',
                finished_at = now()
            FROM workflow_jobs AS job
            WHERE attempt.workflow_job_id = job.id
              AND attempt.attempt_number = job.attempt_count
              AND attempt.status = 'running'
              AND job.status = 'running'
              AND job.lease_expires_at < now()
            """
        )
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = CASE
                    WHEN attempt_count >= max_attempts THEN 'dead'
                    ELSE 'queued'
                END,
                available_at = now(),
                leased_by = NULL,
                lease_expires_at = NULL,
                last_error_code = 'lease_expired',
                completed_at = CASE
                    WHEN attempt_count >= max_attempts THEN now()
                    ELSE NULL
                END,
                updated_at = now()
            WHERE status = 'running'
              AND lease_expires_at < now()
            """
        )
        cursor.execute(
            """
            SELECT queued.id
            FROM workflow_jobs AS queued
            JOIN repositories AS repository
              ON repository.id = queued.repository_id
            WHERE queued.status = 'queued'
              AND repository.enabled = TRUE
              AND queued.available_at <= now()
              AND NOT EXISTS (
                  SELECT 1
                  FROM workflow_jobs AS earlier
                  WHERE earlier.scope_key = queued.scope_key
                    AND earlier.id < queued.id
                    AND earlier.status IN ('queued', 'running')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM workflow_jobs AS running
                  WHERE running.scope_key = queued.scope_key
                    AND running.status = 'running'
              )
            ORDER BY queued.priority DESC, queued.available_at, queued.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        candidate = cursor.fetchone()
        if not candidate:
            return None
        job_id = int(candidate["id"])
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'running',
                attempt_count = attempt_count + 1,
                leased_by = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                last_error_code = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING
                id,
                repository_id,
                pull_request_id,
                job_type,
                scope_key,
                base_revision,
                revision,
                payload,
                attempt_count,
                max_attempts
            """,
            (worker_id, lease_seconds, job_id),
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO workflow_attempts (
                workflow_job_id,
                attempt_number,
                worker_id
            )
            VALUES (%s, %s, %s)
            """,
            (job_id, row["attempt_count"], worker_id),
        )
        return WorkflowJob(
            id=int(row["id"]),
            repository_id=int(row["repository_id"]),
            pull_request_id=(
                int(row["pull_request_id"]) if row["pull_request_id"] is not None else None
            ),
            job_type=row["job_type"],
            scope_key=row["scope_key"],
            base_revision=row["base_revision"],
            revision=row["revision"],
            payload=dict(row["payload"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
        )


def heartbeat_workflow_job(
    conn,
    job_id: int,
    worker_id: str,
    *,
    lease_seconds: int = 900,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE id = %s
              AND status = 'running'
              AND leased_by = %s
              AND lease_expires_at > now()
            """,
            (lease_seconds, job_id, worker_id),
        )
        return cursor.rowcount == 1


def workflow_job_is_current(conn, job_id: int, worker_id: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM workflow_jobs AS job
            JOIN pull_requests AS pull_request ON pull_request.id = job.pull_request_id
            JOIN repositories AS repository ON repository.id = job.repository_id
            WHERE job.id = %s
              AND job.status = 'running'
              AND job.leased_by = %s
              AND job.lease_expires_at > now()
              AND repository.enabled = TRUE
              AND pull_request.state = 'open'
              AND pull_request.base_sha = job.base_revision
              AND pull_request.head_sha = job.revision
              AND NOT EXISTS (
                  SELECT 1
                  FROM workflow_jobs AS newer
                  WHERE newer.scope_key = job.scope_key
                    AND newer.id > job.id
                    AND newer.status IN ('queued', 'running')
              )
            """,
            (job_id, worker_id),
        )
        return cursor.fetchone() is not None


def workflow_job_is_latest(conn, job_id: int, worker_id: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM workflow_jobs AS job
            JOIN repositories AS repository ON repository.id = job.repository_id
            WHERE job.id = %s
              AND job.status = 'running'
              AND job.leased_by = %s
              AND job.lease_expires_at > now()
              AND repository.enabled = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM workflow_jobs AS newer
                  WHERE newer.scope_key = job.scope_key
                    AND newer.id > job.id
                    AND newer.status IN ('queued', 'running')
              )
            """,
            (job_id, worker_id),
        )
        return cursor.fetchone() is not None


def complete_workflow_job(conn, job_id: int, worker_id: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'succeeded',
                leased_by = NULL,
                lease_expires_at = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status = 'running'
              AND leased_by = %s
              AND lease_expires_at > now()
            RETURNING attempt_count
            """,
            (job_id, worker_id),
        )
        row = cursor.fetchone()
        if not row:
            return False
        cursor.execute(
            """
            UPDATE workflow_attempts
            SET status = 'succeeded',
                finished_at = now()
            WHERE workflow_job_id = %s
              AND attempt_number = %s
              AND status = 'running'
            """,
            (job_id, int(row[0])),
        )
        return True


def supersede_workflow_job(conn, job_id: int, worker_id: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = 'superseded',
                leased_by = NULL,
                lease_expires_at = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status = 'running'
              AND leased_by = %s
              AND lease_expires_at > now()
            RETURNING attempt_count
            """,
            (job_id, worker_id),
        )
        row = cursor.fetchone()
        if not row:
            return False
        cursor.execute(
            """
            UPDATE workflow_attempts
            SET status = 'failed',
                error_code = 'superseded',
                finished_at = now()
            WHERE workflow_job_id = %s
              AND attempt_number = %s
              AND status = 'running'
            """,
            (job_id, int(row[0])),
        )
        return True


def fail_workflow_job(
    conn,
    job_id: int,
    worker_id: str,
    error_code: str,
    *,
    retryable: bool = True,
    base_delay_seconds: int = 30,
    max_delay_seconds: int = 1800,
) -> str | None:
    error_code = _validated_error_code(error_code)
    if base_delay_seconds <= 0 or max_delay_seconds <= 0:
        raise ValueError("Retry delays must be positive")

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT attempt_count, max_attempts
            FROM workflow_jobs
            WHERE id = %s
              AND status = 'running'
              AND leased_by = %s
              AND lease_expires_at > now()
            FOR UPDATE
            """,
            (job_id, worker_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        attempt_count, max_attempts = (int(value) for value in row)
        should_retry = retryable and attempt_count < max_attempts
        next_status = "queued" if should_retry else ("dead" if retryable else "failed")
        delay = min(
            max_delay_seconds,
            base_delay_seconds * (2 ** max(0, attempt_count - 1)),
        )
        cursor.execute(
            """
            UPDATE workflow_attempts
            SET status = 'failed',
                error_code = %s,
                finished_at = now()
            WHERE workflow_job_id = %s
              AND attempt_number = %s
              AND status = 'running'
            """,
            (error_code, job_id, attempt_count),
        )
        cursor.execute(
            """
            UPDATE workflow_jobs
            SET status = %s,
                available_at = CASE
                    WHEN %s THEN now() + (%s * interval '1 second')
                    ELSE available_at
                END,
                leased_by = NULL,
                lease_expires_at = NULL,
                last_error_code = %s,
                completed_at = CASE WHEN %s THEN NULL ELSE now() END,
                updated_at = now()
            WHERE id = %s
            """,
            (
                next_status,
                should_retry,
                delay,
                error_code,
                should_retry,
                job_id,
            ),
        )
        return next_status
