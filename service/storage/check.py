"""Durable lifecycle for SCM check runs associated with native reviews."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CHECK_NAME = "Diffuse code review"
CheckConclusion = Literal["cancelled", "failure", "neutral", "skipped", "success"]
VALID_CONCLUSIONS = frozenset(
    {"cancelled", "failure", "neutral", "skipped", "success"}
)

#: How many times Diffuse will try to terminalize one check run before it stops.
#:
#: The reclaim predicate deliberately does not use `status` as its exit: DEV-313
#: is exactly the bug where a `failed` row was the exit, and a single transient
#: PATCH failure therefore stranded a required check permanently. So the bound
#: has to be a count of attempts instead.
#:
#: Eight is chosen against the sweep interval rather than the failure: at one
#: attempt per 300s pass, an unrecoverable check leaves the queue after roughly
#: forty minutes, while a provider incident short of that is still ridden out.
MAX_COMPLETION_ATTEMPTS = 8

#: Recorded on a check Diffuse has stopped trying to complete. Distinct from
#: `check_run_publication_failed`, which describes a single failed attempt: this
#: one says no further attempt will be made, which is what an operator needs to
#: know before reaching for the GitHub UI.
COMPLETION_EXHAUSTED_ERROR_CODE = "check_run_completion_exhausted"


@dataclass(frozen=True)
class CheckRunHandle:
    id: int
    status: str
    external_key: str
    external_id: str | None
    external_url: str | None
    conclusion: str | None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"


def begin_check_run(
    conn,
    *,
    review_run_id: int,
    scm_provider: str,
    head_sha: str,
) -> CheckRunHandle:
    if scm_provider != "github":
        raise ValueError("Status checks require a supported SCM provider")
    if not re.fullmatch(r"[0-9a-f]{40,64}", head_sha):
        raise ValueError("Check-run head SHA must be a lowercase commit digest")
    external_key = f"diffuse-review-run:{review_run_id}"
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT head_sha
            FROM review_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (review_run_id,),
        )
        review = cursor.fetchone()
        if not review:
            raise ValueError("Review run does not exist")
        if review[0] != head_sha:
            raise ValueError("Check-run revision does not match the review run")
        cursor.execute(
            """
            INSERT INTO review_check_runs (
                review_run_id,
                scm_provider,
                head_sha,
                check_name,
                external_key,
                status
            )
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (review_run_id) DO NOTHING
            """,
            (
                review_run_id,
                scm_provider,
                head_sha,
                CHECK_NAME,
                external_key,
            ),
        )
        cursor.execute(
            """
            SELECT
                id,
                status,
                external_key,
                external_id,
                external_url,
                conclusion
            FROM review_check_runs
            WHERE review_run_id = %s
            FOR UPDATE
            """,
            (review_run_id,),
        )
        row = cursor.fetchone()
        if row[1] == "completed":
            return CheckRunHandle(
                id=int(row[0]),
                status=row[1],
                external_key=row[2],
                external_id=row[3],
                external_url=row[4],
                conclusion=row[5],
            )
        cursor.execute(
            """
            UPDATE review_check_runs
            SET status = 'creating',
                attempt_count = attempt_count + 1,
                error_code = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (int(row[0]),),
        )
        return CheckRunHandle(
            id=int(row[0]),
            status="creating",
            external_key=row[2],
            external_id=row[3],
            external_url=row[4],
            conclusion=None,
        )


def get_check_run_for_workflow_job(
    conn,
    workflow_job_id: int,
) -> CheckRunHandle | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                check_run.id,
                check_run.status,
                check_run.external_key,
                check_run.external_id,
                check_run.external_url,
                check_run.conclusion
            FROM review_check_runs AS check_run
            JOIN review_runs AS review ON review.id = check_run.review_run_id
            WHERE review.workflow_job_id = %s
            """,
            (workflow_job_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return CheckRunHandle(
        id=int(row[0]),
        status=row[1],
        external_key=row[2],
        external_id=row[3],
        external_url=row[4],
        conclusion=row[5],
    )


def mark_check_run_started(
    conn,
    check_run_id: int,
    *,
    external_id: str,
    external_url: str | None,
) -> None:
    if not external_id.strip():
        raise ValueError("External check-run ID cannot be empty")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_check_runs
            SET status = 'in_progress',
                external_id = %s,
                external_url = %s,
                error_code = NULL,
                started_at = COALESCE(started_at, now()),
                updated_at = now()
            WHERE id = %s
              AND (
                    status IN ('creating', 'in_progress')
                    OR (status = 'failed' AND external_id IS NULL)
              )
              AND (external_id IS NULL OR external_id = %s)
            """,
            (external_id, external_url, check_run_id, external_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Check run is not in a startable state")


def begin_check_run_completion(conn, check_run_id: int) -> int | None:
    """Count one attempt to terminalize a check and return the running total.

    Called before the remote id is resolved rather than after, because
    rediscovering a check that was created but never persisted is itself a
    network call that can fail forever. Counting only the attempts that got as
    far as the PATCH would leave that path unbounded, which is the shape of the
    bug this whole change is about.

    Returns `None` when the check is already `completed`, which is how a caller
    tells "nothing to do" apart from "attempt recorded".
    """

    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_check_runs
            SET completion_attempts = completion_attempts + 1,
                updated_at = now()
            WHERE id = %s
              AND status <> 'completed'
            RETURNING completion_attempts
            """,
            (check_run_id,),
        )
        row = cursor.fetchone()
    return int(row[0]) if row else None


def mark_check_run_completion_exhausted(conn, check_run_id: int) -> None:
    """Stop retrying a check run, and say so durably.

    Deliberately a distinct error code rather than reusing the per-attempt one:
    an operator reading this row needs to know Diffuse will not try again, so
    the remote check has to be resolved by hand.
    """

    mark_check_run_failed(
        conn,
        check_run_id,
        error_code=COMPLETION_EXHAUSTED_ERROR_CODE,
    )


def mark_check_run_completing(conn, check_run_id: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_check_runs
            SET status = 'completing',
                error_code = NULL,
                updated_at = now()
            WHERE id = %s
              AND status IN ('creating', 'in_progress', 'completing', 'failed')
              AND external_id IS NOT NULL
            """,
            (check_run_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Check run is not in a completable state")


def mark_check_run_completed(
    conn,
    check_run_id: int,
    *,
    conclusion: CheckConclusion,
) -> None:
    if conclusion not in VALID_CONCLUSIONS:
        raise ValueError("Invalid check-run conclusion")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_check_runs
            SET status = 'completed',
                conclusion = %s,
                error_code = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status IN ('in_progress', 'completing', 'failed')
              AND external_id IS NOT NULL
            """,
            (conclusion, check_run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Check run is not in a finishable state")


def mark_check_run_failed(
    conn,
    check_run_id: int,
    *,
    error_code: str = "check_run_publication_failed",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Check-run error code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_check_runs
            SET status = 'failed',
                conclusion = NULL,
                error_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status <> 'completed'
            """,
            (error_code, check_run_id),
        )
