"""Durable generation and publication state for review-thread conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass

import psycopg2.extras

from service.conversation_models import ConversationReference, ConversationTurn
from service.review_models import ReviewFinding


@dataclass(frozen=True)
class ConversationWork:
    id: int
    status: str
    root_comment_id: str
    finding: ReviewFinding
    previous_turns: tuple[ConversationTurn, ...]
    answer: str | None
    references: tuple[ConversationReference, ...]
    index_snapshot_id: int | None
    external_reply_id: str | None
    external_reply_url: str | None

    @property
    def needs_generation(self) -> bool:
        return self.answer is None and self.status != "ignored"

    @property
    def is_published(self) -> bool:
        return self.status == "published"


@dataclass(frozen=True)
class ConversationPublication:
    id: int
    status: str
    answer: str
    references: tuple[ConversationReference, ...]
    external_reply_id: str | None
    external_reply_url: str | None

    @property
    def is_published(self) -> bool:
        return self.status == "published"


@dataclass(frozen=True)
class PublishedConversationReply:
    external_id: str
    external_url: str | None


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
            "file_path": row["finding_file_path"],
            "line": row["finding_line"],
            "side": row["finding_side"],
            "evidence": row["evidence"],
            "suggested_fix": row["suggested_fix"],
        }
    )


def _references(value: object) -> tuple[ConversationReference, ...]:
    if not isinstance(value, list):
        raise RuntimeError("Stored conversation references are not a JSON array")
    return tuple(ConversationReference.model_validate(item) for item in value)


def begin_conversation_generation(
    conn,
    *,
    workflow_job_id: int,
) -> ConversationWork:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                message.id,
                message.status,
                message.root_comment_id,
                message.answer,
                message.code_references,
                message.index_snapshot_id,
                message.external_reply_id,
                message.external_reply_url,
                finding.fingerprint,
                finding.title,
                finding.body,
                finding.severity,
                finding.category,
                finding.security_classification,
                finding.confidence,
                finding.file_path AS finding_file_path,
                finding.line AS finding_line,
                finding.side AS finding_side,
                finding.evidence,
                finding.suggested_fix
            FROM review_conversation_messages AS message
            JOIN finding_threads AS thread
              ON thread.id = message.finding_thread_id
            JOIN finding_lineages AS lineage
              ON lineage.id = thread.lineage_id
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
            WHERE message.workflow_job_id = %s
            FOR UPDATE OF message
            """,
            (workflow_job_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Conversation workflow does not map to a Diffuse thread")
        message_id = int(row["id"])
        status = row["status"]
        if status in {"pending", "generating", "failed"} and row["answer"] is None:
            cursor.execute(
                """
                UPDATE review_conversation_messages
                SET status = 'generating',
                    error_code = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (message_id,),
            )
            status = "generating"
        cursor.execute(
            """
            SELECT author_login, question, answer
            FROM review_conversation_messages
            WHERE finding_thread_id = (
                SELECT finding_thread_id
                FROM review_conversation_messages
                WHERE id = %s
            )
              AND id < %s
              AND status = 'published'
            ORDER BY id DESC
            LIMIT 8
            """,
            (message_id, message_id),
        )
        previous = tuple(reversed(cursor.fetchall()))

    return ConversationWork(
        id=message_id,
        status=status,
        root_comment_id=row["root_comment_id"],
        finding=_finding_from_row(dict(row)),
        previous_turns=tuple(
            ConversationTurn(
                author=item["author_login"],
                question=item["question"],
                answer=item["answer"],
            )
            for item in previous
        ),
        answer=row["answer"],
        references=_references(row["code_references"]),
        index_snapshot_id=(
            int(row["index_snapshot_id"])
            if row["index_snapshot_id"] is not None
            else None
        ),
        external_reply_id=row["external_reply_id"],
        external_reply_url=row["external_reply_url"],
    )


def mark_conversation_ready(
    conn,
    conversation_id: int,
    *,
    answer: str,
    references: tuple[ConversationReference, ...],
    index_snapshot_id: int | None,
    model: str,
    prompt_version: str,
    context_chunk_count: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    if not answer.strip():
        raise ValueError("Conversation answer cannot be empty")
    if min(context_chunk_count, prompt_tokens, completion_tokens) < 0:
        raise ValueError("Conversation usage values cannot be negative")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_conversation_messages
            SET status = 'ready',
                answer = %s,
                code_references = %s,
                index_snapshot_id = %s,
                model = %s,
                prompt_version = %s,
                context_chunk_count = %s,
                prompt_tokens = %s,
                completion_tokens = %s,
                error_code = NULL,
                ready_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status = 'generating'
            """,
            (
                answer,
                psycopg2.extras.Json(
                    [reference.model_dump(mode="json") for reference in references]
                ),
                index_snapshot_id,
                model,
                prompt_version,
                context_chunk_count,
                prompt_tokens,
                completion_tokens,
                conversation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Conversation is not ready for generated output")


def mark_conversation_ignored(
    conn,
    conversation_id: int,
    *,
    reason_code: str,
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
        raise ValueError("Conversation ignore reason is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_conversation_messages
            SET status = 'ignored',
                error_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status NOT IN ('published', 'ignored')
            """,
            (reason_code, conversation_id),
        )


def begin_conversation_publication(
    conn,
    conversation_id: int,
) -> ConversationPublication:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                status,
                answer,
                code_references,
                external_reply_id,
                external_reply_url
            FROM review_conversation_messages
            WHERE id = %s
            FOR UPDATE
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Conversation does not exist")
        if row["answer"] is None:
            raise RuntimeError("Conversation has no generated answer to publish")
        if row["status"] != "published":
            if row["status"] not in {"ready", "publishing", "failed"}:
                raise RuntimeError("Conversation is not publishable")
            cursor.execute(
                """
                UPDATE review_conversation_messages
                SET status = 'publishing',
                    publication_attempt_count = publication_attempt_count + 1,
                    error_code = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (conversation_id,),
            )
            row["status"] = "publishing"
    return ConversationPublication(
        id=int(row["id"]),
        status=row["status"],
        answer=row["answer"],
        references=_references(row["code_references"]),
        external_reply_id=row["external_reply_id"],
        external_reply_url=row["external_reply_url"],
    )


def mark_conversation_published(
    conn,
    conversation_id: int,
    *,
    result: PublishedConversationReply,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_conversation_messages
            SET status = 'published',
                external_reply_id = %s,
                external_reply_url = %s,
                error_code = NULL,
                published_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status IN ('publishing', 'failed')
            """,
            (
                result.external_id,
                result.external_url,
                conversation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Conversation publication is not active")


def mark_conversation_failed(
    conn,
    *,
    workflow_job_id: int,
    error_code: str = "conversation_failed",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Conversation error code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_conversation_messages
            SET status = 'failed',
                error_code = %s,
                updated_at = now()
            WHERE workflow_job_id = %s
              AND status NOT IN ('published', 'ignored')
            """,
            (error_code, workflow_job_id),
        )
