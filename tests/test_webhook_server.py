import hashlib
import hmac
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from service import webhook_server
from service.gitlab import GitLabReviewInteraction
from service.scm import (
    PullRequestEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
)
from service.workflow import EnqueueResult, RepositoryNotOnboardedError

client = TestClient(webhook_server.app)
SECRET = "test-webhook-secret"


def _signed_headers(body: bytes, event: str = "pull_request") -> dict[str, str]:
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "delivery-123",
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)

    response = client.post(
        "/webhook/github",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 401


def test_webhook_rejects_oversized_body_before_parsing(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    body = b"x" * (webhook_server.MAX_WEBHOOK_BODY_BYTES + 1)

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 413


def test_webhook_rejects_oversized_content_length_before_reading(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)

    response = client.post(
        "/webhook/github",
        content=b"{}",
        headers={
            **_signed_headers(b"{}"),
            "Content-Length": str(webhook_server.MAX_WEBHOOK_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413


def test_webhook_durably_queues_normalized_review_work(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    processed: list[tuple] = []

    def fake_enqueue(event, body):
        processed.append((event, body))
        return EnqueueResult(job_id=17, state="queued")

    monkeypatch.setattr(webhook_server, "enqueue_pull_request", fake_enqueue)
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "head": {"sha": "a" * 40, "ref": "feature/auth"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "octocat"},
            "draft": False,
            "labels": [{"name": "needs-review"}],
            "title": "Protect tenant boundaries",
            "body": "Adds authorization checks.",
            "changed_files": 4,
            "created_at": "2026-07-23T14:00:00Z",
            "updated_at": "2026-07-23T15:30:00Z",
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["job_id"] == 17
    assert response.json()["revision"] == "a" * 40
    assert len(processed) == 1
    event, accepted_body = processed[0]
    assert event.repo_full_name == "owner/repo"
    assert event.number == 42
    assert event.delivery_id == "delivery-123"
    assert event.author == "octocat"
    assert event.base_branch == "main"
    assert event.head_branch == "feature/auth"
    assert event.labels == ("needs-review",)
    assert event.source_created_at == "2026-07-23T14:00:00+00:00"
    assert event.metadata_complete
    assert accepted_body == body


def test_webhook_ignores_diffuse_managed_description_update(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)

    def unexpected_enqueue(_event, _body):
        raise AssertionError("Managed description update must not queue a review")

    monkeypatch.setattr(
        webhook_server,
        "enqueue_pull_request",
        unexpected_enqueue,
    )
    human_body = "Adds authorization checks."
    managed_body = (
        f"{human_body}\n\n"
        "<!-- diffuse-review-description:start -->\n"
        f"<!-- diffuse-review:42:{'a' * 40} -->\n"
        "## Diffuse code review\n"
        "<!-- diffuse-review-description:end -->"
    )
    payload = {
        "action": "edited",
        "changes": {"body": {"from": human_body}},
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "head": {"sha": "a" * 40, "ref": "feature/auth"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "octocat"},
            "draft": False,
            "labels": [],
            "title": "Protect tenant boundaries",
            "body": managed_body,
            "changed_files": 4,
            "created_at": "2026-07-23T14:00:00Z",
            "updated_at": "2026-07-23T15:31:00Z",
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "diffuse_description_update",
    }


def test_webhook_reports_merged_pull_request_lifecycle_as_recorded(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    processed: list[PullRequestEvent] = []

    def fake_enqueue(event, _body):
        processed.append(event)
        return EnqueueResult(job_id=None, state="pull_request_merged")

    monkeypatch.setattr(webhook_server, "enqueue_pull_request", fake_enqueue)
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "head": {"sha": "a" * 40, "ref": "feature/auth"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "octocat"},
            "draft": False,
            "labels": [],
            "title": "Protect tenant boundaries",
            "body": "Adds authorization checks.",
            "changed_files": 4,
            "created_at": "2026-07-23T14:00:00Z",
            "updated_at": "2026-07-23T15:30:00Z",
            "closed_at": "2026-07-23T15:30:00Z",
            "merged_at": "2026-07-23T15:29:59Z",
            "merged": True,
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "recorded"
    assert response.json()["queue_state"] == "pull_request_merged"
    assert response.json()["job_id"] is None
    assert len(processed) == 1
    assert processed[0].state == "merged"
    assert processed[0].source_closed_at == "2026-07-23T15:30:00+00:00"
    assert processed[0].source_merged_at == "2026-07-23T15:29:59+00:00"


def test_webhook_rejects_review_for_repository_that_is_not_onboarded(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)

    def fake_enqueue(_event, _body):
        raise RepositoryNotOnboardedError("owner/repo")

    monkeypatch.setattr(webhook_server, "enqueue_pull_request", fake_enqueue)
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "head": {"sha": "c" * 40, "ref": "feature/auth"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "octocat"},
            "draft": False,
            "labels": [],
            "title": "Protect tenant boundaries",
            "body": None,
            "changed_files": 4,
            "created_at": "2026-07-23T14:00:00Z",
            "updated_at": "2026-07-23T15:31:00Z",
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 409


def test_ping_is_signature_verified(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    body = b'{"zen":"Keep it logically awesome."}'

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="ping"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_requires_a_current_database_schema(monkeypatch):
    monkeypatch.setattr(
        webhook_server,
        "_verify_database_schema",
        lambda: None,
    )
    ready = client.get("/ready")

    def unavailable():
        raise RuntimeError("schema is stale")

    monkeypatch.setattr(
        webhook_server,
        "_verify_database_schema",
        unavailable,
    )
    stale = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert stale.status_code == 503
    assert stale.json() == {
        "status": "not_ready",
        "reason": "database_schema",
    }


def _manual_comment_payload(*, association: str = "MEMBER") -> dict:
    return {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 42, "pull_request": {"url": "unused"}},
        "comment": {
            "id": 991,
            "body": "@diffuse check the authorization boundary",
            "created_at": "2026-07-23T16:00:00Z",
            "author_association": association,
            "user": {"login": "reviewer", "type": "User"},
        },
    }


def _manual_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 42,
            "web_url": "https://github.com/owner/repo/pull/42",
            "action": "manual",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T16:00:00Z",
            "delivery_id": "delivery-123",
            "author": "contributor",
            "base_branch": "main",
            "head_branch": "feature/auth",
            "is_draft": True,
            "labels": ["do-not-auto-review"],
            "title": "Draft authorization changes",
            "description": "Early feedback requested.",
            "trigger_kind": "manual",
            "trigger_id": "issue-comment:991",
            "metadata_complete": True,
            "changed_file_count": 4,
        }
    )


def _gitlab_headers(event: str = "Merge Request Hook") -> dict[str, str]:
    return {
        "X-Gitlab-Event": event,
        "X-Gitlab-Token": "gitlab-webhook-secret",
        "X-Gitlab-Event-UUID": "gitlab-delivery-123",
        "X-Gitlab-Instance": "https://gitlab.example.com",
        "Content-Type": "application/json",
    }


def _gitlab_payload(action: str = "open") -> dict:
    return {
        "object_kind": "merge_request",
        "project": {
            "id": 91,
            "path_with_namespace": "group/repo",
            "web_url": "https://gitlab.example.com/group/repo",
            "default_branch": "main",
        },
        "object_attributes": {"iid": 17, "action": action},
        "changes": {},
    }


def _gitlab_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "group/repo",
            "number": 17,
            "web_url": "https://gitlab.example.com/group/repo/-/merge_requests/17",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "gitlab-delivery-123",
            "author": "contributor",
            "base_branch": "main",
            "head_branch": "feature/auth",
            "is_draft": False,
            "labels": [],
            "title": "Protect tenant boundaries",
            "description": "",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 1,
            "state": "open",
            "source_created_at": "2026-07-23T14:00:00Z",
            "source_closed_at": "",
            "source_merged_at": "",
            "additions": 0,
            "deletions": 0,
        }
    )


def _gitlab_review_interaction() -> GitLabReviewInteraction:
    feedback = ReviewFeedbackCommentEvent(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        repo_full_name="group/repo",
        number=17,
        delivery_id="gitlab-delivery-123",
        external_comment_id="401",
        root_comment_id="202",
        author="reviewer",
        author_association="COLLABORATOR",
        created_at="2026-07-23T18:30:00Z",
        body="@diffuse why is this unsafe?",
        file_path="service/read.py",
    )
    conversation = ReviewConversationEvent(
        provider="gitlab",
        scm_base_url="https://gitlab.example.com",
        api_base_url="https://gitlab.example.com/api/v4",
        repo_full_name="group/repo",
        number=17,
        delivery_id="gitlab-delivery-123",
        external_comment_id="401",
        root_comment_id="202",
        head_sha="a" * 40,
        base_sha="b" * 40,
        comment_commit_sha="a" * 40,
        author="reviewer",
        author_association="COLLABORATOR",
        created_at="2026-07-23T18:30:00Z",
        question="why is this unsafe?",
        file_path="service/read.py",
        line=12,
        side="RIGHT",
        diff_hunk="",
        thread_id="discussion-1",
    )
    return GitLabReviewInteraction(
        feedback=feedback,
        conversation=conversation,
    )


def _configure_gitlab(monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "gitlab-webhook-secret")
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.example.com/api/v4")


def test_gitlab_webhook_enriches_then_durably_queues_review(monkeypatch):
    _configure_gitlab(monkeypatch)
    processed: list[tuple] = []
    fetch = AsyncMock(return_value=_gitlab_event())

    def fake_enqueue(event, body):
        processed.append((event, body))
        return EnqueueResult(job_id=81, state="queued")

    monkeypatch.setattr(webhook_server, "fetch_gitlab_merge_request_event", fetch)
    monkeypatch.setattr(webhook_server, "enqueue_pull_request", fake_enqueue)
    body = json.dumps(_gitlab_payload()).encode()

    response = client.post(
        "/webhook/gitlab",
        content=body,
        headers=_gitlab_headers(),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["job_id"] == 81
    assert len(processed) == 1
    assert processed[0][0].provider == "gitlab"
    assert processed[0][1] == body
    fetch.assert_awaited_once()


def test_gitlab_webhook_ignores_approval_without_fetching_metadata(monkeypatch):
    _configure_gitlab(monkeypatch)
    fetch = AsyncMock()
    monkeypatch.setattr(webhook_server, "fetch_gitlab_merge_request_event", fetch)
    body = json.dumps(_gitlab_payload(action="approved")).encode()

    response = client.post(
        "/webhook/gitlab",
        content=body,
        headers=_gitlab_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "action=approved"}
    fetch.assert_not_awaited()


def test_gitlab_webhook_queues_default_branch_push(monkeypatch):
    _configure_gitlab(monkeypatch)
    processed: list = []

    def fake_enqueue(event, _body):
        processed.append(event)
        return EnqueueResult(job_id=82, state="queued")

    monkeypatch.setattr(webhook_server, "enqueue_repository_push", fake_enqueue)
    payload = _gitlab_payload()
    payload.update(
        {
            "ref": "refs/heads/main",
            "before": "b" * 40,
            "after": "a" * 40,
            "event_created_at": "2026-07-23T15:30:00Z",
            "commits": [],
        }
    )
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/gitlab",
        content=body,
        headers=_gitlab_headers(event="Push Hook"),
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == 82
    assert len(processed) == 1
    assert processed[0].provider == "gitlab"


def test_gitlab_note_webhook_records_feedback_and_queues_conversation(monkeypatch):
    _configure_gitlab(monkeypatch)
    interaction = _gitlab_review_interaction()
    fetch = AsyncMock(return_value=interaction)
    recorded: list[tuple] = []
    queued: list[tuple] = []
    monkeypatch.setattr(
        webhook_server,
        "fetch_gitlab_review_interaction",
        fetch,
    )
    monkeypatch.setattr(
        webhook_server,
        "record_review_feedback",
        lambda event, body: recorded.append((event, body)) or "recorded",
    )
    monkeypatch.setattr(
        webhook_server,
        "enqueue_review_conversation",
        lambda event, body: (
            queued.append((event, body))
            or EnqueueResult(job_id=83, state="queued")
        ),
    )
    payload = {
        "object_kind": "note",
        "project": {
            "id": 91,
            "path_with_namespace": "group/repo",
            "web_url": "https://gitlab.example.com/group/repo",
        },
        "object_attributes": {
            "id": 401,
            "action": "create",
            "noteable_type": "MergeRequest",
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/gitlab",
        content=body,
        headers=_gitlab_headers(event="Note Hook"),
    )

    assert response.status_code == 202
    assert response.json()["trigger"] == "review_conversation"
    assert response.json()["job_id"] == 83
    assert response.json()["feedback_state"] == "recorded"
    assert recorded == [(interaction.feedback, body)]
    assert queued == [(interaction.conversation, body)]
    fetch.assert_awaited_once()


def test_gitlab_note_webhook_queues_authorized_manual_review(monkeypatch):
    _configure_gitlab(monkeypatch)
    payload_values = _gitlab_event().to_payload()
    payload_values.update(
        {
            "action": "manual",
            "trigger_kind": "manual",
            "trigger_id": "note:401",
            "updated_at": "2026-07-23T18:30:00Z",
        }
    )
    manual_event = PullRequestEvent.from_payload(payload_values)
    interaction = GitLabReviewInteraction(
        feedback=None,
        conversation=None,
        manual_review=manual_event,
        manual_requested_by="reviewer",
    )
    fetch = AsyncMock(return_value=interaction)
    queued: list[tuple] = []
    monkeypatch.setattr(
        webhook_server,
        "fetch_gitlab_review_interaction",
        fetch,
    )
    monkeypatch.setattr(
        webhook_server,
        "enqueue_pull_request",
        lambda event, body: (
            queued.append((event, body))
            or EnqueueResult(job_id=84, state="queued")
        ),
    )
    payload = {
        "object_kind": "note",
        "project": {
            "id": 91,
            "path_with_namespace": "group/repo",
            "web_url": "https://gitlab.example.com/group/repo",
        },
        "object_attributes": {
            "id": 401,
            "action": "create",
            "noteable_type": "MergeRequest",
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/gitlab",
        content=body,
        headers=_gitlab_headers(event="Note Hook"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "trigger": "manual",
        "requested_by": "reviewer",
        "repo": "group/repo",
        "pr": 17,
        "revision": "a" * 40,
        "delivery": "gitlab-delivery-123",
        "job_id": 84,
        "queue_state": "queued",
    }
    assert queued == [(manual_event, body)]
    fetch.assert_awaited_once()


def test_gitlab_webhook_rejects_invalid_legacy_token(monkeypatch):
    _configure_gitlab(monkeypatch)
    headers = _gitlab_headers()
    headers["X-Gitlab-Token"] = "wrong"

    response = client.post(
        "/webhook/gitlab",
        content=b"{}",
        headers=headers,
    )

    assert response.status_code == 401


def _review_conversation_payload(**comment_overrides) -> dict:
    comment = {
        "id": 1201,
        "in_reply_to_id": 901,
        "body": "@diffuse Why does this bypass the tenant check?",
        "created_at": "2026-07-23T17:00:00Z",
        "author_association": "MEMBER",
        "user": {"login": "reviewer", "type": "User"},
        "path": "service/auth.py",
        "line": 42,
        "side": "RIGHT",
        "diff_hunk": "@@ -41,1 +41,2 @@\n+return account",
        "commit_id": "a" * 40,
    }
    comment.update(comment_overrides)
    return {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "state": "open",
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
        },
        "comment": comment,
    }


def test_webhook_queues_authorized_review_conversation(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued: list[tuple] = []
    monkeypatch.setattr(
        webhook_server,
        "enqueue_review_conversation",
        lambda event, body: (
            queued.append((event, body))
            or EnqueueResult(job_id=33, state="queued")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "record_review_feedback",
        lambda _event, _body: "recorded",
    )
    payload = _review_conversation_payload()
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="pull_request_review_comment"),
    )

    assert response.status_code == 202
    assert response.json()["trigger"] == "review_conversation"
    assert response.json()["thread_root"] == "901"
    assert response.json()["job_id"] == 33
    assert response.json()["feedback_state"] == "recorded"
    assert queued[0][0].question == "Why does this bypass the tenant check?"
    assert queued[0][1] == body


def test_webhook_ignores_acknowledgement_without_queueing(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued = []
    monkeypatch.setattr(
        webhook_server,
        "enqueue_review_conversation",
        lambda *args: queued.append(args),
    )
    recorded = []
    monkeypatch.setattr(
        webhook_server,
        "record_review_feedback",
        lambda *args: recorded.append(args) or "recorded",
    )
    payload = _review_conversation_payload(body="@diffuse thanks")
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="pull_request_review_comment"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    assert response.json()["trigger"] == "review_feedback"
    assert queued == []
    assert len(recorded) == 1


def test_webhook_records_context_reply_without_invoking_conversation(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued = []
    recorded = []
    monkeypatch.setattr(
        webhook_server,
        "enqueue_review_conversation",
        lambda *args: queued.append(args),
    )
    monkeypatch.setattr(
        webhook_server,
        "record_review_feedback",
        lambda *args: recorded.append(args) or "recorded",
    )
    payload = _review_conversation_payload(
        body="We intentionally use the domain-layer transaction here."
    )
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="pull_request_review_comment"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    assert response.json()["feedback_state"] == "recorded"
    assert queued == []
    assert recorded[0][0].body.startswith("We intentionally")


def test_webhook_queues_authorized_manual_review(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    event = _manual_event()
    fetch = AsyncMock(return_value=event)
    queued: list[tuple] = []
    monkeypatch.setattr(webhook_server, "fetch_manual_pull_request_event", fetch)
    monkeypatch.setattr(
        webhook_server,
        "enqueue_pull_request",
        lambda accepted_event, body: (
            queued.append((accepted_event, body))
            or EnqueueResult(job_id=29, state="queued")
        ),
    )
    payload = _manual_comment_payload()
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="issue_comment"),
    )

    assert response.status_code == 202
    assert response.json()["trigger"] == "manual"
    assert response.json()["requested_by"] == "reviewer"
    assert response.json()["job_id"] == 29
    fetch.assert_awaited_once()
    assert queued == [(event, body)]


def test_webhook_ignores_unauthorized_manual_review(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    fetch = AsyncMock()
    monkeypatch.setattr(webhook_server, "fetch_manual_pull_request_event", fetch)
    payload = _manual_comment_payload(association="NONE")
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="issue_comment"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    fetch.assert_not_awaited()


def test_default_branch_push_queues_exact_index_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    queued = []

    def fake_enqueue(event, body):
        queued.append((event, body))
        return EnqueueResult(job_id=23, state="queued")

    monkeypatch.setattr(webhook_server, "enqueue_repository_push", fake_enqueue)
    payload = {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "deleted": False,
        "repository": {
            "full_name": "owner/repo",
            "default_branch": "main",
            "pushed_at": 1784835000,
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="push"),
    )

    assert response.status_code == 202
    assert response.json()["revision"] == "b" * 40
    assert response.json()["job_id"] == 23
    assert queued[0][0].ref_name == "refs/heads/main"
    assert queued[0][1] == body


def test_non_default_branch_push_is_ignored(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    payload = {
        "ref": "refs/heads/feature",
        "before": "a" * 40,
        "after": "b" * 40,
        "deleted": False,
        "repository": {
            "full_name": "owner/repo",
            "default_branch": "main",
            "pushed_at": 1784835000,
        },
    }
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers=_signed_headers(body, event="push"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
