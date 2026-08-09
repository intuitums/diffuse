"""PostgreSQL coverage for repository-level automatic review control."""

from __future__ import annotations

import os
from contextlib import closing

import psycopg2
import pytest

from service.hosted.workflow import enqueue_review_conversation_event, enqueue_review_event
from service.repositories import (
    RepositoryIdentityConflictError,
    get_repository,
    get_repository_by_full_name,
    register_repository,
    resolve_github_repository,
    set_repository_auto_review,
)
from service.scm import (
    PullRequestEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
)
from service.storage.feedback import record_review_comment_feedback


def _event(
    repository: str,
    *,
    delivery_id: str,
    trigger_kind: str = "automatic",
    github_repository_id: int = 0,
) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repository,
            "number": 17,
            "web_url": f"https://github.com/{repository}/pull/17",
            "action": "manual" if trigger_kind == "manual" else "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-08-09T12:00:00Z",
            "delivery_id": delivery_id,
            "github_repository_id": github_repository_id,
            "trigger_kind": trigger_kind,
            "trigger_id": "operator-request" if trigger_kind == "manual" else "",
        }
    )


def test_disabling_automatic_review_preserves_manual_reviews():
    repository_name = "settings/automatic-review"
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repository_name,
            default_branch="main",
        )
        assert repository.auto_review is True
        assert set_repository_auto_review(connection, repository.id, False)
        assert get_repository(connection, repository.id).auto_review is False

        automatic = enqueue_review_event(
            connection,
            _event(repository_name, delivery_id="automatic-review-disabled"),
            payload_sha256="a" * 64,
        )
        manual = enqueue_review_event(
            connection,
            _event(
                repository_name,
                delivery_id="manual-review-enabled",
                trigger_kind="manual",
            ),
            payload_sha256="b" * 64,
        )

        assert automatic.job_id is None
        assert automatic.state == "auto_review_disabled"
        assert manual.accepted
        assert manual.job_id is not None

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM workflow_jobs WHERE repository_id = %s",
                (repository.id,),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT count(*) FROM scm_webhook_deliveries WHERE scm_base_url = %s",
                ("https://github.com",),
            )
            assert cursor.fetchone()[0] >= 2


def test_github_identity_renames_the_existing_repository_without_losing_settings():
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="old-owner/old-name",
            default_branch="main",
        )
        set_repository_auto_review(connection, repository.id, False)
        bound = resolve_github_repository(
            connection,
            scm_base_url="https://github.com",
            github_repository_id=101,
            full_name="old-owner/old-name",
        )
        renamed = resolve_github_repository(
            connection,
            scm_base_url="https://github.com",
            github_repository_id=101,
            full_name="new-owner/new-name",
        )

        assert bound.id == renamed.id == repository.id
        assert renamed.github_repository_id == 101
        assert renamed.auto_review is False
        assert renamed.clone_url == "https://github.com/new-owner/new-name.git"
        assert get_repository_by_full_name(
            connection,
            "old-owner/old-name",
            scm_base_url="https://github.com",
        ) is None
        assert get_repository_by_full_name(
            connection,
            "new-owner/new-name",
            scm_base_url="https://github.com",
        ).id == repository.id

        other = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="other-owner/other-name",
            default_branch="main",
        )
        resolve_github_repository(
            connection,
            scm_base_url="https://github.com",
            github_repository_id=202,
            full_name=other.full_name,
        )
        with pytest.raises(RepositoryIdentityConflictError, match="conflicts"):
            resolve_github_repository(
                connection,
                scm_base_url="https://github.com",
                github_repository_id=101,
                full_name=other.full_name,
            )


def test_review_comment_webhooks_update_a_renamed_repository_by_github_identity():
    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        conversation_repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="old-owner/conversation",
            default_branch="main",
            github_repository_id=301,
        )
        conversation = ReviewConversationEvent(
            provider="github",
            scm_base_url="https://github.com",
            api_base_url="https://api.github.com",
            repo_full_name="new-owner/conversation",
            github_repository_id=301,
            number=17,
            delivery_id="rename-conversation",
            external_comment_id="701",
            root_comment_id="601",
            head_sha="a" * 40,
            base_sha="b" * 40,
            comment_commit_sha="a" * 40,
            author="reviewer",
            author_association="MEMBER",
            created_at="2026-08-09T12:00:00Z",
            question="Why is this safe?",
            file_path="service/app.py",
            line=10,
            side="RIGHT",
            diff_hunk="@@ -10 +10 @@",
        )
        result = enqueue_review_conversation_event(
            connection,
            conversation,
            payload_sha256="c" * 64,
        )
        assert result.state == "ignored:not_diffuse_thread"
        assert get_repository(connection, conversation_repository.id).full_name == (
            "new-owner/conversation"
        )

        feedback_repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="old-owner/feedback",
            default_branch="main",
            github_repository_id=302,
        )
        feedback = ReviewFeedbackCommentEvent(
            provider="github",
            scm_base_url="https://github.com",
            api_base_url="https://api.github.com",
            repo_full_name="new-owner/feedback",
            github_repository_id=302,
            number=17,
            delivery_id="rename-feedback",
            external_comment_id="702",
            root_comment_id="602",
            author="reviewer",
            author_association="MEMBER",
            created_at="2026-08-09T12:00:00Z",
            body="This is intentional.",
            file_path="service/app.py",
        )
        assert (
            record_review_comment_feedback(
                connection,
                feedback,
                payload_sha256="d" * 64,
            )
            == "ignored:not_diffuse_thread"
        )
        assert get_repository(connection, feedback_repository.id).full_name == (
            "new-owner/feedback"
        )
