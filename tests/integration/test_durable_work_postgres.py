"""Durability guarantees the review queue makes once a job has gone wrong.

Every case here is a state a job can only reach by *failing*, which is exactly
why none of them were covered: the happy-path integration tests never produce a
`dead` row, a stranded review run, or a retired idempotency key.
"""

import os
from contextlib import closing

import psycopg2
import pytest

from indexer.store import activate_snapshot, begin_index_snapshot
from service.cross_repository import (
    CrossRepositoryContextError,
    DroppedContextRepository,
    create_repository_cluster,
    record_dropped_context_repositories,
    resolve_cross_repository_context,
)
from service.repositories import register_repository
from service.review_store import begin_review_run
from service.scm import PullRequestEvent, PushEvent
from service.workflow import (
    claim_stranded_review_jobs,
    claim_workflow_job,
    enqueue_repository_index_event,
    enqueue_review_event,
    fail_workflow_job,
)

MODEL = "integration-durable-work-model"
DIMENSIONS = 1536


def _push_event(*, delivery: str, after: str, pushed_at: str) -> PushEvent:
    return PushEvent(
        provider="github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        repo_full_name="durable/index-repo",
        ref_name="refs/heads/main",
        default_branch="main",
        before_sha="0" * 40,
        after_sha=after,
        pushed_at=pushed_at,
        delivery_id=delivery,
    )


def _review_event(*, delivery: str, head: str, updated_at: str) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "durable/review-repo",
            "number": 41,
            "web_url": "https://github.com/durable/review-repo/pull/41",
            "action": "synchronize",
            "head_sha": head,
            "base_sha": "b" * 40,
            "updated_at": updated_at,
            "delivery_id": delivery,
        }
    )


def _job_status(connection, job_id: int) -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status, idempotency_key FROM workflow_jobs WHERE id = %s",
            (job_id,),
        )
        return cursor.fetchone()


def test_context_repository_outside_a_cluster_is_dropped_and_audited():
    """`context.repos` names a repository the operator never clustered.

    `.diffuse` is committed to the repository under review, so this is the one
    input on the review path that anyone with merge access can write. Honouring
    it would let a low-value repository pull a high-value one's indexed source
    into its review context.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]

    with closing(psycopg2.connect(database_url)) as connection:
        repositories = {
            name: register_repository(
                connection,
                scm_provider="github",
                scm_base_url="https://github.com",
                full_name=f"durable-context/{name}",
                default_branch="main",
            )
            for name in ("app", "clustered", "unrelated")
        }
        snapshots = {}
        for ordinal, name in enumerate(("app", "clustered", "unrelated"), start=1):
            snapshot = begin_index_snapshot(
                connection,
                f"durable-context/{name}",
                str(ordinal) * 40,
                MODEL,
                DIMENSIONS,
            )
            assert activate_snapshot(connection, snapshot.snapshot_id)
            snapshots[name] = snapshot
        create_repository_cluster(
            connection,
            name="durable-stack",
            repository_ids=(
                repositories["app"].id,
                repositories["clustered"].id,
            ),
            actor_login="durable-operator",
        )

        resolution = resolve_cross_repository_context(
            connection,
            primary_repository_id=repositories["app"].id,
            primary_snapshot_id=snapshots["app"].snapshot_id,
            explicit_repositories=(
                "durable-context/clustered",
                "durable-context/unrelated",
            ),
            model=MODEL,
            dimensions=DIMENSIONS,
        )

        assert [
            item.repository_full_name
            for item in resolution.plan.related_snapshots
        ] == ["durable-context/clustered"]
        assert resolution.dropped_repositories == (
            DroppedContextRepository(
                repository_full_name="durable-context/unrelated",
                reason_code="not_cluster_member",
            ),
        )

        record_dropped_context_repositories(
            connection,
            primary_repository_id=repositories["app"].id,
            actor_label="durable-worker",
            dropped=resolution.dropped_repositories,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT actor_kind, actor_label, action, resource_id, details
                FROM audit_events
                WHERE repository_id = %s
                  AND action = 'review.context_repository_dropped'
                """,
                (repositories["app"].id,),
            )
            assert cursor.fetchall() == [
                (
                    "system",
                    "durable-worker",
                    "review.context_repository_dropped",
                    "durable-context/unrelated",
                    {"reason": "not_cluster_member"},
                )
            ]

        # A repository that is not onboarded at all is still a hard
        # configuration error, unchanged by the authorization gate.
        with pytest.raises(CrossRepositoryContextError, match="same SCM host"):
            resolve_cross_repository_context(
                connection,
                primary_repository_id=repositories["app"].id,
                primary_snapshot_id=snapshots["app"].snapshot_id,
                explicit_repositories=("durable-context/absent",),
                model=MODEL,
                dimensions=DIMENSIONS,
            )
        connection.rollback()


def test_a_dead_index_job_does_not_pin_its_commit_forever():
    """A commit whose index job died must be indexable again.

    `workflow_jobs.idempotency_key` is globally unique with no TTL, so before
    the fix a dead job left the repository serving a stale index that neither a
    re-push nor an operator-requested index could ever refresh.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    push = _push_event(
        delivery="durable-index-1",
        after="1" * 40,
        pushed_at="2026-07-23T16:00:00Z",
    )
    repush = _push_event(
        delivery="durable-index-2",
        after="1" * 40,
        pushed_at="2026-07-23T16:05:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="durable/index-repo",
            default_branch="main",
        )
        first = enqueue_repository_index_event(
            connection,
            push,
            payload_sha256="1" * 64,
        )
        claimed = claim_workflow_job(connection, "durable-worker", lease_seconds=60)
        assert claimed is not None
        assert (
            fail_workflow_job(
                connection,
                claimed.id,
                "durable-worker",
                "indexing_failed",
                retryable=False,
            )
            == "failed"
        )

        again = enqueue_repository_index_event(
            connection,
            repush,
            payload_sha256="2" * 64,
        )

        assert again.accepted
        assert again.job_id != first.job_id
        status, retired_key = _job_status(connection, first.job_id)
        assert status == "failed"
        assert retired_key.endswith(f"#retired-{first.job_id}")
        replacement = claim_workflow_job(connection, "durable-worker-2", lease_seconds=60)
        assert replacement is not None
        assert replacement.id == again.job_id
        assert replacement.revision == "1" * 40
        connection.rollback()


def test_a_succeeded_revision_is_still_deduplicated():
    """Only failure re-opens a revision; a completed one stays deduplicated."""
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    push = _push_event(
        delivery="durable-index-3",
        after="2" * 40,
        pushed_at="2026-07-23T16:00:00Z",
    )
    repush = _push_event(
        delivery="durable-index-4",
        after="2" * 40,
        pushed_at="2026-07-23T16:05:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="durable/index-repo",
            default_branch="main",
        )
        first = enqueue_repository_index_event(
            connection,
            push,
            payload_sha256="3" * 64,
        )
        duplicate = enqueue_repository_index_event(
            connection,
            repush,
            payload_sha256="4" * 64,
        )

        assert duplicate.job_id == first.job_id
        assert duplicate.state == "duplicate_revision:queued"
        connection.rollback()


def test_lease_expiry_backs_a_repeatedly_abandoned_job_off():
    """A job that keeps killing its worker must not re-claim instantly.

    The first expiry is usually a deploy, so it stays immediate; the second is
    the poison-job signal and earns the same backoff an observed failure gets.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    event = _review_event(
        delivery="durable-review-lease",
        head="a" * 40,
        updated_at="2026-07-23T15:30:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        begin_index_snapshot(
            connection,
            "durable/review-repo",
            "e" * 40,
            MODEL,
            DIMENSIONS,
        )
        enqueued = enqueue_review_event(connection, event, payload_sha256="5" * 64)
        for expected_attempt in (1, 2):
            claimed = claim_workflow_job(connection, "durable-worker", lease_seconds=60)
            assert claimed is not None
            assert claimed.attempt_count == expected_attempt
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE workflow_jobs
                    SET lease_expires_at = now() - interval '1 second'
                    WHERE id = %s
                    """,
                    (claimed.id,),
                )

        # Sweeping the second expiry defers the job instead of handing it
        # straight back, so this claim finds nothing.
        assert claim_workflow_job(connection, "durable-worker", lease_seconds=60) is None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT available_at > now() FROM workflow_jobs WHERE id = %s",
                (enqueued.job_id,),
            )
            assert cursor.fetchone()[0] is True
        connection.rollback()


def test_lease_swept_review_is_reconciled_instead_of_blocking_forever():
    """The lease sweep can declare a job dead with nobody watching.

    `run_once`'s handler is the only other reader of `dead`, and it never runs
    for a swept job, so its review run stays `generating` and its required check
    stays `in_progress` with no explanation and no way out.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    event = _review_event(
        delivery="durable-review-stranded",
        head="c" * 40,
        updated_at="2026-07-23T15:35:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        snapshot = begin_index_snapshot(
            connection,
            "durable/review-repo",
            "f" * 40,
            MODEL,
            DIMENSIONS,
        )
        assert activate_snapshot(connection, snapshot.snapshot_id)
        enqueued = enqueue_review_event(connection, event, payload_sha256="6" * 64)
        claimed = claim_workflow_job(connection, "durable-worker", lease_seconds=60)
        assert claimed is not None
        begin_review_run(
            connection,
            workflow_job_id=claimed.id,
            repository_id=claimed.repository_id,
            pull_request_id=claimed.pull_request_id,
            index_snapshot_id=snapshot.snapshot_id,
            base_sha=claimed.base_revision,
            head_sha=claimed.revision,
            model="durable-review-model",
            prompt_version="durable-prompt-v1",
            context_fingerprint="d" * 64,
        )
        with connection.cursor() as cursor:
            # Exhaust the attempts and expire the lease, exactly as a worker
            # that was killed mid-review would leave them.
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET attempt_count = max_attempts,
                    lease_expires_at = now() - interval '1 second'
                WHERE id = %s
                """,
                (claimed.id,),
            )
        assert claim_workflow_job(connection, "durable-worker-2", lease_seconds=60) is None
        assert _job_status(connection, enqueued.job_id)[0] == "dead"

        # Freshly dead jobs belong to `run_once`, which is finalizing them right
        # about now; only one left behind past the grace period is stranded.
        assert claim_stranded_review_jobs(connection, grace_seconds=300) == ()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE workflow_jobs
                SET completed_at = now() - interval '10 minutes'
                WHERE id = %s
                """,
                (enqueued.job_id,),
            )
        stranded = claim_stranded_review_jobs(connection, grace_seconds=300)

        assert [(item.id, item.retries_exhausted) for item in stranded] == [
            (enqueued.job_id, True)
        ]
        assert stranded[0].payload["head_sha"] == "c" * 40
        connection.rollback()
