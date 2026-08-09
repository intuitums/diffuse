import httpx
import pytest

from service.github.api import (
    fetch_manual_pull_request_event,
    fetch_pull_request_commits,
    fetch_pull_request_diff,
    fetch_pull_request_update_diff,
    normalize_manual_review_request,
    normalize_pull_request_event,
    normalize_review_conversation_event,
    normalize_review_feedback_comment_event,
)
from service.scm import (
    PullRequestEvent,
    ReviewConversationEvent,
    ReviewFeedbackCommentEvent,
)


def _comment_payload(**comment_overrides) -> dict:
    comment = {
        "id": 991,
        "body": "@diffuse review this draft",
        "created_at": "2026-07-23T16:00:00Z",
        "author_association": "MEMBER",
        "user": {"login": "reviewer", "type": "User"},
    }
    comment.update(comment_overrides)
    return {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 42, "pull_request": {"url": "unused"}},
        "comment": comment,
    }


def _pull_request_json() -> dict:
    return {
        "number": 42,
        "html_url": "https://github.com/owner/repo/pull/42",
        "head": {"sha": "a" * 40, "ref": "feature/auth"},
        "base": {"sha": "b" * 40, "ref": "main"},
        "user": {"login": "contributor"},
        "draft": True,
        "labels": [{"name": "do-not-auto-review"}],
        "title": "Draft authorization changes",
        "body": "Early feedback requested.",
        "changed_files": 7,
        "created_at": "2026-07-23T14:00:00Z",
        "updated_at": "2026-07-23T15:30:00Z",
        "state": "open",
    }


def _review_comment_payload(**comment_overrides) -> dict:
    comment = {
        "id": 1201,
        "in_reply_to_id": 901,
        "body": "@diffuse Why can this bypass the tenant check?",
        "created_at": "2026-07-23T17:00:00Z",
        "author_association": "COLLABORATOR",
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


def _github_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 42,
            "web_url": "https://github.com/owner/repo/pull/42",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T16:00:00Z",
            "delivery_id": "delivery-commits",
        }
    )


def test_pull_request_event_preserves_github_repository_identity():
    event = normalize_pull_request_event(
        {
            "repository": {"full_name": "owner/repo", "id": 987654321},
            "pull_request": _pull_request_json(),
        },
        delivery_id="repository-identity",
        action="opened",
    )

    assert event.github_repository_id == 987654321
    assert "github_repository:987654321" in event.scope_key


def test_review_conversation_normalizes_explicit_authorized_question():
    payload = _review_comment_payload()
    payload["repository"]["id"] = 123456
    event = normalize_review_conversation_event(
        payload,
        delivery_id="conversation-delivery-1",
    )

    assert event is not None
    assert event.question == "Why can this bypass the tenant check?"
    assert event.root_comment_id == "901"
    assert event.file_path == "service/auth.py"
    assert event.github_repository_id == 123456
    assert "github_repository:123456" in event.scope_key
    assert event.scope_key.endswith("pull_request:42:review_thread:901")
    assert ReviewConversationEvent.from_payload(event.to_payload()) == event
    legacy_payload = event.to_payload()
    legacy_payload.pop("github_repository_id")
    assert ReviewConversationEvent.from_payload(legacy_payload).github_repository_id == 0


@pytest.mark.parametrize(
    "comment_overrides",
    [
        {"body": "Human reviewers: what do you think?"},
        {"body": "@diffuse-bot explain this"},
        {"body": "@diffuse thanks"},
        {"body": "[Human discussion only] @diffuse please stay quiet"},
        {"author_association": "NONE"},
        {"user": {"login": "automation", "type": "Bot"}},
        {"in_reply_to_id": None},
        # Diffuse's own replies arrive as ordinary user comments under a user
        # access token, so the marker — not the actor type — has to exclude them.
        {
            "body": "@diffuse follow-up\n<!-- diffuse-conversation:c1 -->",
            "user": {"login": "operator", "type": "User"},
            "author_association": "OWNER",
        },
    ],
)
def test_review_conversation_ignores_non_questions_and_unauthorized_comments(
    comment_overrides,
):
    assert (
        normalize_review_conversation_event(
            _review_comment_payload(**comment_overrides),
            delivery_id="conversation-delivery-ignored",
        )
        is None
    )


def test_review_feedback_records_authorized_context_without_a_mention():
    payload = _review_comment_payload(
        body="This is intentional because the caller already scopes the tenant."
    )
    payload["repository"]["id"] = 123456
    event = normalize_review_feedback_comment_event(
        payload,
        delivery_id="feedback-delivery-1",
    )

    assert event is not None
    assert event.root_comment_id == "901"
    assert event.author_association == "COLLABORATOR"
    assert event.github_repository_id == 123456
    assert event.event_key == "reply:1201:created"
    assert isinstance(event, ReviewFeedbackCommentEvent)


@pytest.mark.parametrize(
    "comment_overrides",
    [
        {"body": "[Human discussion only] do not learn from this"},
        {"author_association": "NONE"},
        {"user": {"login": "automation", "type": "Bot"}},
        {"in_reply_to_id": None},
        # A Diffuse-authored thread update must never be re-ingested as human
        # feedback about the finding it describes.
        {
            "body": "Marked this finding as addressed.\n"
            "<!-- diffuse-thread-operation:t1 -->",
            "user": {"login": "operator", "type": "User"},
            "author_association": "OWNER",
        },
    ],
)
def test_review_feedback_ignores_excluded_or_unauthorized_comments(
    comment_overrides,
):
    assert (
        normalize_review_feedback_comment_event(
            _review_comment_payload(**comment_overrides),
            delivery_id="feedback-delivery-ignored",
        )
        is None
    )


@pytest.mark.anyio
async def test_manual_comment_fetches_fresh_pr_metadata_and_preserves_trigger_identity(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    manual_request = normalize_manual_review_request(_comment_payload())
    assert manual_request is not None
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_pull_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_manual_pull_request_event(
            manual_request,
            delivery_id="manual-delivery-1",
            client=client,
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/repos/owner/repo/pulls/42"
    assert requests[0].headers["Authorization"] == "Bearer test-token"
    assert event.trigger_kind == "manual"
    assert event.trigger_id == "issue-comment:991"
    assert event.is_draft
    assert event.author == "contributor"
    assert event.labels == ("do-not-auto-review",)
    assert event.updated_at == "2026-07-23T16:00:00+00:00"
    assert event.source_created_at == "2026-07-23T14:00:00+00:00"
    assert event.metadata_complete
    assert event.changed_file_count == 7


@pytest.mark.parametrize(
    "comment_overrides",
    [
        {"body": "Looks good to me"},
        {"author_association": "NONE"},
        {"user": {"login": "automation", "type": "Bot"}},
    ],
)
def test_manual_trigger_requires_command_collaborator_and_human(comment_overrides):
    assert normalize_manual_review_request(_comment_payload(**comment_overrides)) is None


def test_manual_trigger_ignores_regular_issue_comments():
    payload = _comment_payload()
    payload["issue"].pop("pull_request")

    assert normalize_manual_review_request(payload) is None


@pytest.mark.anyio
async def test_update_diff_is_pinned_between_reviewed_heads(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 42,
            "web_url": "https://github.com/owner/repo/pull/42",
            "action": "synchronize",
            "head_sha": "c" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T17:00:00Z",
            "delivery_id": "update-diff",
        }
    )
    requests: list[httpx.Request] = []
    diff = (
        "diff --git a/service/api.py b/service/api.py\n"
        "--- a/service/api.py\n"
        "+++ b/service/api.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=diff)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_pull_request_update_diff(
            event,
            "a" * 40,
            client=client,
        )

    assert result == diff
    assert requests[0].url.path == (
        "/repos/owner/repo/compare/"
        + ("a" * 40)
        + "..."
        + ("c" * 40)
    )
    assert requests[0].headers["Accept"] == "application/vnd.github.diff"


@pytest.mark.anyio
async def test_fetch_pull_request_commits_returns_bounded_provenance_fields(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "a" * 40,
                    "commit": {
                        "message": (
                            "Implement tenant checks\n\n"
                            "Co-Authored-By: Claude <noreply@anthropic.com>"
                        ),
                        "author": {
                            "name": "Fischer",
                            "email": "developer@example.com",
                        },
                        "committer": {
                            "name": "GitHub",
                            "email": "noreply@github.com",
                        },
                        "verification": {"verified": True},
                    },
                    "author": {"login": "fschrhunt", "type": "User"},
                    "committer": {"login": "web-flow", "type": "User"},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_pull_request_commits(_github_event(), client=client)

    assert result.complete
    assert len(result.commits) == 1
    assert result.commits[0].message.endswith("<noreply@anthropic.com>")
    assert result.commits[0].author_login == "fschrhunt"
    assert result.commits[0].verified
    assert requests[0].url.path == "/repos/owner/repo/pulls/42/commits"
    assert requests[0].url.params["per_page"] == "100"
    assert requests[0].headers["Authorization"] == "Bearer test-token"


@pytest.mark.anyio
async def test_fetch_pull_request_commits_marks_stale_metadata_incomplete():
    value = {
        "sha": "c" * 40,
        "commit": {
            "message": "Older commit",
            "author": {"name": "User", "email": "user@example.com"},
            "committer": {"name": "User", "email": "user@example.com"},
            "verification": {"verified": False},
        },
        "author": {"login": "user", "type": "User"},
        "committer": {"login": "user", "type": "User"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[value])
        )
    ) as client:
        result = await fetch_pull_request_commits(_github_event(), client=client)

    assert not result.complete


@pytest.mark.anyio
async def test_manual_trigger_rejects_closed_pull_request():
    manual_request = normalize_manual_review_request(_comment_payload())
    assert manual_request is not None
    value = _pull_request_json()
    value["state"] = "closed"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=value)
        )
    ) as client:
        with pytest.raises(ValueError, match="open pull request"):
            await fetch_manual_pull_request_event(
                manual_request,
                delivery_id="manual-delivery-1",
                client=client,
            )


@pytest.mark.anyio
async def test_github_diff_fetch_uses_the_configured_scm_timeout(monkeypatch):
    """The owned client must honor SCM_API_TIMEOUT_SECONDS, not a hardcoded value."""
    monkeypatch.setenv("SCM_API_TIMEOUT_SECONDS", "7.5")
    observed: dict = {}
    real_client = httpx.AsyncClient

    def record_timeout(*args, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(
            lambda _request: httpx.Response(200, text="diff --git a/a b/a")
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", record_timeout)

    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 42,
            "web_url": "https://github.com/owner/repo/pull/42",
            "action": "synchronize",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T16:00:00Z",
            "delivery_id": "timeout-delivery-1",
        }
    )
    diff = await fetch_pull_request_diff(event)

    assert diff.startswith("diff --git")
    assert observed["timeout"] == 7.5


def test_quoting_a_diffuse_review_still_triggers_a_manual_review():
    """A maintainer quoting a Diffuse comment is a human request, not Diffuse.

    Diffuse must not treat its own comments as manual triggers, but matching the
    marker anywhere in the body also dropped a quote-reply -- the most natural way
    to ask for a re-review of a specific finding. Diffuse opens its own comments
    with the marker, so the check is anchored at the start instead.
    """
    quoted = (
        "> <!-- diffuse-review-failure -->\n"
        "> ## Diffuse could not complete this review\n"
        "\n"
        "@diffuse review please retry this one\n"
    )

    request = normalize_manual_review_request(_comment_payload(body=quoted))

    assert request is not None
    assert request.number == 42


def test_diffuse_own_comment_is_not_a_manual_trigger():
    """The marker at the start still identifies Diffuse's own comment."""
    own = (
        "<!-- diffuse-review-failure -->\n"
        "## Diffuse could not complete this review\n"
        "\n"
        "@diffuse review\n"
    )

    assert normalize_manual_review_request(_comment_payload(body=own)) is None
