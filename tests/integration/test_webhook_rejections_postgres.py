import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from service.repositories import register_repository
from service.scm import PullRequestEvent
from service.workflow import (
    REPOSITORY_NOT_ONBOARDED_REASON,
    RepositoryNotOnboardedError,
    enqueue_review_event,
    record_webhook_rejection,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
        reason="POSTGRES_TEST_DATABASE_URL is not configured",
    ),
]


def _event(*, repo: str, delivery: str) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repo,
            "number": 7,
            "web_url": f"https://github.com/{repo}/pull/7",
            "action": "synchronize",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-25T05:30:00Z",
            "delivery_id": delivery,
        }
    )


def _cleanup(connection, *, repo: str, delivery: str) -> None:
    """Leave no rows behind.

    Several existing integration tests assert against unscoped `SELECT`s (e.g.
    `pull_request_lifecycle_events` with no WHERE clause), so a test that
    enqueues successfully must clean up or it breaks them by pollution.
    Deleting the repository cascades through pull_requests to the lifecycle
    events; deliveries carry no repository FK and are removed by id.
    """
    with connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM repositories WHERE full_name = %s", (repo,))
        cursor.execute(
            "DELETE FROM scm_webhook_deliveries WHERE delivery_id = %s",
            (delivery,),
        )
        cursor.execute(
            "DELETE FROM scm_webhook_rejections WHERE delivery_id = %s",
            (delivery,),
        )


def _rejections(connection, delivery: str):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT repo_full_name, reason, attempts, event_name
            FROM scm_webhook_rejections
            WHERE delivery_id = %s
            """,
            (delivery,),
        )
        return cursor.fetchall()


def test_a_rejection_is_recorded_and_retries_increment_attempts():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex[:12]
    repo = f"reject/{suffix}"
    delivery = f"delivery-{suffix}"

    with closing(psycopg2.connect(database_url)) as connection:
        try:
            # The repository was never onboarded, so the enqueue must refuse it.
            with pytest.raises(RepositoryNotOnboardedError):
                enqueue_review_event(
                    connection,
                    _event(repo=repo, delivery=delivery),
                    payload_sha256="1" * 64,
                )
            connection.rollback()

            for _ in range(3):
                with connection:
                    record_webhook_rejection(
                        connection,
                        scm_provider="github",
                        scm_base_url="https://github.com",
                        delivery_id=delivery,
                        event_name="pull_request",
                        repo_full_name=repo,
                        reason=REPOSITORY_NOT_ONBOARDED_REASON,
                    )

            rows = _rejections(connection, delivery)
            assert rows == [(repo, REPOSITORY_NOT_ONBOARDED_REASON, 3, "pull_request")]
        finally:
            _cleanup(connection, repo=repo, delivery=delivery)


def test_a_recorded_rejection_does_not_block_the_delivery_once_onboarded():
    """The whole reason rejections live in their own table.

    `scm_webhook_deliveries` is UNIQUE on the delivery id, and a miss there is
    treated as a duplicate that must not be enqueued. If a rejection were
    recorded in that table, onboarding the repository and letting GitHub retry
    would silently never produce a review.
    """
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex[:12]
    repo = f"recover/{suffix}"
    delivery = f"delivery-{suffix}"
    event = _event(repo=repo, delivery=delivery)

    with closing(psycopg2.connect(database_url)) as connection:
        try:
            with pytest.raises(RepositoryNotOnboardedError):
                enqueue_review_event(connection, event, payload_sha256="1" * 64)
            connection.rollback()

            with connection:
                record_webhook_rejection(
                    connection,
                    scm_provider="github",
                    scm_base_url="https://github.com",
                    delivery_id=delivery,
                    event_name="pull_request",
                    repo_full_name=repo,
                    reason=REPOSITORY_NOT_ONBOARDED_REASON,
                )
            assert _rejections(connection, delivery)

            # The operator onboards the repository; GitHub redelivers the same id.
            with connection:
                register_repository(
                    connection,
                    scm_provider="github",
                    scm_base_url="https://github.com",
                    full_name=repo,
                    default_branch="main",
                )
            with connection:
                result = enqueue_review_event(
                    connection,
                    event,
                    payload_sha256="1" * 64,
                )

            assert result.accepted, f"redelivery was not enqueued: {result.state}"
            assert result.job_id is not None

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM scm_webhook_deliveries "
                    "WHERE delivery_id = %s",
                    (delivery,),
                )
                assert cursor.fetchone()[0] == 1
        finally:
            _cleanup(connection, repo=repo, delivery=delivery)


def test_rejection_recording_validates_its_inputs():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    with closing(psycopg2.connect(database_url)) as connection:
        for kwargs, expected in (
            ({"scm_provider": "svn"}, "provider is invalid"),
            ({"reason": "because"}, "reason is invalid"),
            ({"delivery_id": ""}, "delivery id is invalid"),
            ({"event_name": ""}, "event name is invalid"),
            ({"repo_full_name": ""}, "repository name is invalid"),
        ):
            payload = {
                "scm_provider": "github",
                "scm_base_url": "https://github.com",
                "delivery_id": "d1",
                "event_name": "pull_request",
                "repo_full_name": "a/b",
                "reason": REPOSITORY_NOT_ONBOARDED_REASON,
                **kwargs,
            }
            with pytest.raises(ValueError, match=expected):
                record_webhook_rejection(connection, **payload)
