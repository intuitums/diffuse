import pytest

from service.scm import (
    PLAINTEXT_ORIGIN_VARIABLE,
    FeedbackSyncEvent,
    PullRequestEvent,
    PushEvent,
    ReviewFeedbackCommentEvent,
    normalize_base_url,
    normalize_timestamp,
)


def _payload() -> dict:
    return {
        "provider": "github",
        "scm_base_url": "https://github.example.com",
        "api_base_url": "https://github.example.com/api/v3",
        "repo_full_name": "owner/repo",
        "number": 7,
        "web_url": "https://github.example.com/owner/repo/pull/7",
        "action": "synchronize",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "updated_at": "2026-07-23T15:30:00Z",
        "delivery_id": "delivery-7",
        "author": "octocat",
        "base_branch": "main",
        "head_branch": "feature/auth",
        "is_draft": False,
        "labels": ["needs-review"],
        "title": "Protect tenant boundaries",
        "description": "Adds authorization checks.",
        "trigger_kind": "automatic",
        "trigger_id": "",
        "metadata_complete": True,
        "changed_file_count": 4,
    }


def test_pull_request_event_has_stable_scope_and_revision_idempotency():
    event = PullRequestEvent.from_payload(_payload())

    assert event.scope_key == ("github:https://github.example.com:owner/repo:pull_request:7")
    assert event.idempotency_key == (
        f"{event.scope_key}:{'b' * 40}:{'a' * 40}:{event.trigger_fingerprint}"
    )
    assert PullRequestEvent.from_payload(event.to_payload()) == event


def test_pull_request_event_preserves_authoritative_lifecycle_timestamps():
    event = PullRequestEvent.from_payload(
        {
            **_payload(),
            "state": "merged",
            "source_created_at": "2026-07-22T15:30:00-05:00",
            "source_closed_at": "2026-07-23T15:30:01Z",
            "source_merged_at": "2026-07-23T15:30:00Z",
            "additions": 12,
            "deletions": 3,
        }
    )

    assert event.source_created_at == "2026-07-22T20:30:00+00:00"
    assert event.source_closed_at == "2026-07-23T15:30:01+00:00"
    assert event.source_merged_at == "2026-07-23T15:30:00+00:00"
    assert event.lifecycle_at == event.source_merged_at
    assert PullRequestEvent.from_payload(event.to_payload()) == event


def test_pull_request_event_rejects_inconsistent_lifecycle_timestamps():
    with pytest.raises(ValueError, match="do not match"):
        PullRequestEvent.from_payload(
            {
                **_payload(),
                "state": "open",
                "source_created_at": "2026-07-22T15:30:00Z",
                "source_closed_at": "2026-07-23T15:30:00Z",
                "source_merged_at": "",
                "additions": 0,
                "deletions": 0,
            }
        )


def test_event_payload_schema_is_closed():
    payload = _payload()
    payload["token"] = "must-not-be-accepted"

    with pytest.raises(ValueError, match="schema"):
        PullRequestEvent.from_payload(payload)


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/github",
        "https://user:password@github.example.com",
        "https://github.example.com?token=secret",
        "https://github.example.com/%2e%2e/admin",
    ],
)
def test_base_url_rejects_unsafe_shapes(value):
    with pytest.raises(ValueError, match="absolute HTTP"):
        normalize_base_url(value, field_name="SCM_URL")


@pytest.mark.parametrize(
    "value",
    [
        "http://github.example.com",
        "http://github.example.com/api/v3",
        "http://192.0.2.10:8080",
        "http://127.0.0.1.evil.example.com",
    ],
)
def test_base_url_rejects_plaintext_non_loopback_origins(monkeypatch, value):
    monkeypatch.delenv(PLAINTEXT_ORIGIN_VARIABLE, raising=False)
    with pytest.raises(ValueError, match="must use https"):
        normalize_base_url(value, field_name="SCM_URL")


@pytest.mark.parametrize(
    "value",
    [
        "https://github.example.com",
        "http://localhost:8000",
        "http://127.0.0.1:53123",
        "http://[::1]:8000",
    ],
)
def test_base_url_accepts_tls_and_loopback_plaintext(monkeypatch, value):
    monkeypatch.delenv(PLAINTEXT_ORIGIN_VARIABLE, raising=False)
    assert normalize_base_url(value, field_name="SCM_URL") == value


def test_base_url_plaintext_opt_out_is_explicit(monkeypatch):
    monkeypatch.setenv(PLAINTEXT_ORIGIN_VARIABLE, "1")
    assert (
        normalize_base_url("http://github.example.com", field_name="SCM_URL")
        == "http://github.example.com"
    )

    monkeypatch.setenv(PLAINTEXT_ORIGIN_VARIABLE, "yes")
    with pytest.raises(ValueError, match=f"{PLAINTEXT_ORIGIN_VARIABLE} must be 0 or 1"):
        normalize_base_url("http://github.example.com", field_name="SCM_URL")


def test_timestamp_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        normalize_timestamp("2026-07-23T15:30:00")


def test_push_event_supports_nested_namespaces_and_exact_commit_identity():
    event = PushEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com/",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "group/platform/repo",
            "ref_name": "refs/heads/main",
            "default_branch": "main",
            "before_sha": "a" * 40,
            "after_sha": "b" * 40,
            "pushed_at": "2026-07-23T15:30:00Z",
            "delivery_id": "push-1",
        }
    )

    assert event.scope_key.endswith("group/platform/repo:repository_index:refs/heads/main")
    assert event.idempotency_key == f"{event.scope_key}:{'b' * 40}"
    assert PushEvent.from_payload(event.to_payload()) == event


def test_feedback_events_have_scoped_safe_identities():
    comment = ReviewFeedbackCommentEvent(
        provider="github",
        scm_base_url="https://github.example.com",
        api_base_url="https://github.example.com/api/v3",
        repo_full_name="owner/repo",
        number=7,
        delivery_id="feedback-comment-1",
        external_comment_id="301",
        root_comment_id="201",
        author="reviewer",
        author_association="MEMBER",
        created_at="2026-07-23T18:00:00Z",
        body="This is intentional in our domain layer.",
        file_path="service/domain.py",
    )
    sync = FeedbackSyncEvent(
        provider="github",
        scm_base_url="https://github.example.com",
        api_base_url="https://github.example.com/api/v3",
        repo_full_name="owner/repo",
        number=7,
        root_comment_id="201",
        generation=3,
        base_sha="b" * 40,
        head_sha="a" * 40,
    )

    assert comment.event_key == "reply:301:created"
    assert sync.scope_key.endswith("pull_request:7:feedback_thread:201")
    assert sync.idempotency_key.endswith("feedback_thread:201:generation:3")
    assert FeedbackSyncEvent.from_payload(sync.to_payload()) == sync
