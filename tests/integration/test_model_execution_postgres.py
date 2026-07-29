from __future__ import annotations

import os
import uuid
from contextlib import closing

import psycopg2

from service.model_execution import StructuredGenerationResult
from service.review_models import CandidateBatch
from service.review_store import (
    begin_review_run,
    load_review_generation_step,
    mark_review_failed,
    save_review_generation_step,
)


def test_generation_steps_survive_review_retry_and_require_exact_fingerprint():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    with closing(psycopg2.connect(database_url)) as connection:
        with connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO repositories (
                    scm_provider,
                    scm_base_url,
                    full_name,
                    default_branch
                )
                VALUES ('github', 'https://github.com', %s, 'main')
                RETURNING id
                """,
                (f"model-execution/{suffix}",),
            )
            repository_id = int(cursor.fetchone()[0])
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
                    title,
                    description,
                    latest_event_at
                )
                VALUES (%s, 1, %s, %s, %s, 'author', 'main', 'feature',
                        'Fixture', '', now())
                RETURNING id
                """,
                (
                    repository_id,
                    f"https://github.com/model-execution/{suffix}/pull/1",
                    "a" * 40,
                    "b" * 40,
                ),
            )
            pull_request_id = int(cursor.fetchone()[0])
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
                    status,
                    leased_by,
                    lease_expires_at
                )
                VALUES (%s, %s, 'review_pull_request', %s, %s, %s, %s, '{}',
                        'running', 'model-execution-test', now() + interval '5 minutes')
                RETURNING id
                """,
                (
                    repository_id,
                    pull_request_id,
                    f"model-execution:{suffix}",
                    f"github:model-execution:{suffix}:1",
                    "a" * 40,
                    "b" * 40,
                ),
            )
            workflow_job_id = int(cursor.fetchone()[0])

        with connection:
            review = begin_review_run(
                connection,
                workflow_job_id=workflow_job_id,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                index_snapshot_id=None,
                base_sha="a" * 40,
                head_sha="b" * 40,
                model="fixture",
                prompt_version="fixture-v1",
                context_fingerprint="c" * 64,
                executor="codex-cli",
                execution_plan={"schema_version": "diffuse-execution-plan-v1"},
                execution_plan_fingerprint="d" * 64,
            )
            result = StructuredGenerationResult(
                value=CandidateBatch(
                    analysis_summary="No actionable issue.",
                    findings=[],
                ),
                prompt_tokens=12,
                completion_tokens=3,
                resolved_model="fixture",
                executor_version="fixture-cli/1",
            )
            save_review_generation_step(
                connection,
                review_run_id=review.id,
                step_key="candidate/security/0",
                request_fingerprint="e" * 64,
                response_schema="service.review_models.CandidateBatch",
                result=result,
            )
            mark_review_failed(connection, review.id)

        with connection:
            resumed = begin_review_run(
                connection,
                workflow_job_id=workflow_job_id,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                index_snapshot_id=None,
                base_sha="a" * 40,
                head_sha="b" * 40,
                model="fixture",
                prompt_version="fixture-v1",
                context_fingerprint="c" * 64,
                executor="codex-cli",
                execution_plan={"schema_version": "diffuse-execution-plan-v1"},
                execution_plan_fingerprint="d" * 64,
            )
            stored = load_review_generation_step(
                connection,
                review_run_id=resumed.id,
                step_key="candidate/security/0",
                request_fingerprint="e" * 64,
            )
            stale = load_review_generation_step(
                connection,
                review_run_id=resumed.id,
                step_key="candidate/security/0",
                request_fingerprint="f" * 64,
            )

        with connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM repositories WHERE id = %s", (repository_id,))

        assert resumed.id == review.id
        assert stored is not None
        assert stored.response["analysis_summary"] == "No actionable issue."
        assert stored.prompt_tokens == 12
        assert stale is None
