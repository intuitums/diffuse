"""Durable idempotency for automatic SCM approval decisions and publication."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg2.extras

from service.auto_approval import AutoApprovalDecision


@dataclass(frozen=True)
class AutoApprovalHandle:
    id: int
    status: str
    eligible: bool
    idempotency_key: str
    external_id: str | None
    external_url: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in {"ineligible", "published", "cancelled"}


def begin_auto_approval(
    conn,
    *,
    review_run_id: int,
    scm_provider: str,
    head_sha: str,
    policy_fingerprint: str,
    decision: AutoApprovalDecision,
) -> AutoApprovalHandle:
    """Record the immutable decision and claim an eligible approval attempt."""
    idempotency_key = f"review-run:{review_run_id}:auto-approval:{head_sha}"
    initial_status = "publishing" if decision.eligible else "ineligible"
    initial_attempts = 1 if decision.eligible else 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT status, head_sha
            FROM review_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (review_run_id,),
        )
        review_run = cursor.fetchone()
        if not review_run or review_run["status"] != "published":
            raise RuntimeError(
                "Automatic approval requires a published review run"
            )
        if review_run["head_sha"] != head_sha:
            raise ValueError(
                "Automatic approval head does not match the review run"
            )
        cursor.execute(
            """
            INSERT INTO review_auto_approvals (
                review_run_id,
                scm_provider,
                head_sha,
                policy_fingerprint,
                eligible,
                decision_reason,
                risk_level,
                risk_ceiling,
                changed_paths,
                changed_file_count,
                changed_line_count,
                diff_chars,
                idempotency_key,
                status,
                attempt_count
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (review_run_id) DO NOTHING
            """,
            (
                review_run_id,
                scm_provider,
                head_sha,
                policy_fingerprint,
                decision.eligible,
                decision.reason_code,
                decision.risk_level.value,
                decision.risk_ceiling.value,
                list(decision.changed_paths),
                decision.changed_file_count,
                decision.changed_line_count,
                decision.diff_chars,
                idempotency_key,
                initial_status,
                initial_attempts,
            ),
        )
        created = cursor.rowcount == 1
        cursor.execute(
            """
            SELECT *
            FROM review_auto_approvals
            WHERE review_run_id = %s
            FOR UPDATE
            """,
            (review_run_id,),
        )
        row = cursor.fetchone()
        expected = {
            "scm_provider": scm_provider,
            "head_sha": head_sha,
            "policy_fingerprint": policy_fingerprint,
            "eligible": decision.eligible,
            "decision_reason": decision.reason_code,
            "risk_level": decision.risk_level.value,
            "risk_ceiling": decision.risk_ceiling.value,
            "changed_paths": list(decision.changed_paths),
            "changed_file_count": decision.changed_file_count,
            "changed_line_count": decision.changed_line_count,
            "diff_chars": decision.diff_chars,
            "idempotency_key": idempotency_key,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise RuntimeError(
                "Stored automatic-approval decision conflicts with this retry"
            )
        if row["status"] in {"ineligible", "published", "cancelled"}:
            return AutoApprovalHandle(
                id=int(row["id"]),
                status=row["status"],
                eligible=bool(row["eligible"]),
                idempotency_key=row["idempotency_key"],
                external_id=row["external_id"],
                external_url=row["external_url"],
            )
        if not created:
            cursor.execute(
                """
                UPDATE review_auto_approvals
                SET status = 'publishing',
                    attempt_count = attempt_count + 1,
                    error_code = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (int(row["id"]),),
            )
        return AutoApprovalHandle(
            id=int(row["id"]),
            status="publishing",
            eligible=True,
            idempotency_key=row["idempotency_key"],
            external_id=row["external_id"],
            external_url=row["external_url"],
        )


def mark_auto_approval_published(
    conn,
    approval_id: int,
    *,
    external_id: str,
    external_url: str | None,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_auto_approvals
            SET status = 'published',
                external_id = %s,
                external_url = %s,
                error_code = NULL,
                published_at = now(),
                updated_at = now()
            WHERE id = %s
              AND eligible = TRUE
              AND status IN ('publishing', 'published')
            """,
            (external_id, external_url, approval_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Automatic approval is not publishable")


def mark_auto_approval_failed(
    conn,
    approval_id: int,
    *,
    error_code: str = "approval_publication_failed",
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_auto_approvals
            SET status = 'failed',
                error_code = %s,
                updated_at = now()
            WHERE id = %s
              AND eligible = TRUE
              AND status = 'publishing'
            """,
            (error_code, approval_id),
        )


def mark_auto_approval_cancelled(
    conn,
    approval_id: int,
    *,
    error_code: str,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_auto_approvals
            SET status = 'cancelled',
                error_code = %s,
                updated_at = now()
            WHERE id = %s
              AND eligible = TRUE
              AND status IN ('publishing', 'failed')
            """,
            (error_code, approval_id),
        )
