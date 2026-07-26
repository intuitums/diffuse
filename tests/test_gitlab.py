import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from service.gitlab import (
    GitLabMetadataPendingError,
    fetch_gitlab_merge_request_event,
    fetch_gitlab_review_interaction,
    fetch_manual_gitlab_merge_request_event,
    gitlab_merge_request_action,
    normalize_gitlab_push_event,
    verify_gitlab_webhook,
)
from service.review_interaction import ManualReviewRequest

SIGNING_KEY = b"gitlab-standard-webhook-test-key"
SIGNING_TOKEN = "whsec_" + base64.b64encode(SIGNING_KEY).decode()


def _standard_headers(body: bytes, *, now: datetime, webhook_id: str = "msg_123") -> dict:
    timestamp = str(int(now.timestamp()))
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(
        hmac.new(SIGNING_KEY, signed, hashlib.sha256).digest()
    ).decode()
    return {
        "webhook_id": webhook_id,
        "webhook_timestamp": timestamp,
        "webhook_signature": f"v1,invalid v1,{signature}",
    }


def _webhook_payload(*, action: str = "open") -> dict:
    return {
        "object_kind": "merge_request",
        "project": {
            "id": 91,
            "path_with_namespace": "group/subgroup/repo",
            "web_url": "https://gitlab.example.com/group/subgroup/repo",
            "default_branch": "main",
        },
        "object_attributes": {
            "iid": 17,
            "action": action,
        },
        "changes": {},
    }


def _merge_request_json(**overrides) -> dict:
    value = {
        "project_id": 91,
        "source_project_id": 122,
        "iid": 17,
        "web_url": (
            "https://gitlab.example.com/group/subgroup/repo/-/merge_requests/17"
        ),
        "author": {"username": "contributor"},
        "target_branch": "main",
        "source_branch": "feature/tenant-check",
        "draft": False,
        "labels": ["backend", "security"],
        "title": "Protect tenant reads",
        "description": "Adds the missing authorization condition.",
        "state": "opened",
        "created_at": "2026-07-23T14:00:00.000Z",
        "updated_at": "2026-07-23T15:30:00.000Z",
        "closed_at": None,
        "merged_at": None,
        "changes_count": "4",
        "sha": "a" * 40,
        "diff_refs": {
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "start_sha": "b" * 40,
        },
    }
    value.update(overrides)
    return value


def _note_payload(body: str = "@diffuse why is this unsafe?") -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {
            "id": 8,
            "username": "reviewer",
            "name": "Reviewer",
        },
        "project_id": 91,
        "project": {
            "id": 91,
            "path_with_namespace": "group/subgroup/repo",
            "web_url": "https://gitlab.example.com/group/subgroup/repo",
            "default_branch": "main",
        },
        "object_attributes": {
            "id": 401,
            "internal": False,
            "note": body,
            "noteable_type": "MergeRequest",
            "author_id": 8,
            "created_at": "2026-07-23T18:30:00.000Z",
            "system": False,
            "action": "create",
        },
        "merge_request": {
            "iid": 17,
            "state": "opened",
            "target_project_id": 91,
        },
    }


def _note_discussion(body: str = "@diffuse why is this unsafe?") -> dict:
    return {
        "id": "discussion-1",
        "notes": [
            {
                "id": 202,
                "body": (
                    "Finding details\n\n"
                    f"<!-- diffuse-finding:{'f' * 64} -->"
                ),
                "position": {
                    "base_sha": "b" * 40,
                    "head_sha": "a" * 40,
                    "start_sha": "b" * 40,
                    "old_path": "service/read.py",
                    "new_path": "service/read.py",
                    "old_line": None,
                    "new_line": 12,
                },
            },
            {
                "id": 401,
                "body": body,
            },
        ],
    }


def _top_level_note_discussion(body: str = "@diffuse review this") -> dict:
    return {
        "id": "discussion-top-level",
        "notes": [
            {
                "id": 401,
                "body": body,
                "position": None,
            },
        ],
    }


def _verified(monkeypatch):
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.example.com")
    monkeypatch.setenv(
        "GITLAB_API_URL",
        "https://gitlab.example.com/api/v4",
    )
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    return verify_gitlab_webhook(
        b"{}",
        legacy_token="legacy-secret",
        event_uuid="event-uuid-1",
        instance_header="https://gitlab.example.com",
    )


def test_standard_webhook_signature_is_replay_bounded_and_host_aware(monkeypatch):
    now = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    body = b'{"object_kind":"merge_request"}'
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", SIGNING_TOKEN)
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.example.com")

    verified = verify_gitlab_webhook(
        body,
        **_standard_headers(body, now=now),
        instance_header="https://gitlab.example.com",
        now=now,
    )

    assert verified.delivery_id == "msg_123"
    assert verified.authentication == "standard_webhooks"
    assert verified.scm_base_url == "https://gitlab.example.com"
    assert verified.api_base_url == "https://gitlab.example.com/api/v4"

    stale = _standard_headers(body, now=now - timedelta(minutes=6))
    with pytest.raises(HTTPException) as error:
        verify_gitlab_webhook(body, **stale, now=now)
    assert error.value.status_code == 401


def test_invalid_standard_signature_never_downgrades_to_legacy(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SIGNING_TOKEN", SIGNING_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    now = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)

    with pytest.raises(HTTPException) as error:
        verify_gitlab_webhook(
            b"{}",
            webhook_id="msg_123",
            webhook_timestamp=str(int(now.timestamp())),
            webhook_signature="v1,bad",
            legacy_token="legacy-secret",
            now=now,
        )

    assert error.value.status_code == 401


def test_non_ascii_legacy_token_is_rejected_as_unauthenticated(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.com")

    with pytest.raises(HTTPException) as error:
        verify_gitlab_webhook(
            b"{}",
            legacy_token="legacy-secr\xe9t",
            event_uuid="event-123",
        )

    assert error.value.status_code == 401


def test_legacy_webhook_requires_stable_identity_and_allowlisted_instance(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.com")
    monkeypatch.setenv(
        "GITLAB_ALLOWED_INSTANCES",
        "https://gitlab.example.com,https://code.example.net",
    )

    verified = verify_gitlab_webhook(
        b"{}",
        legacy_token="legacy-secret",
        idempotency_key="delivery-123",
        instance_header="https://gitlab.example.com/",
    )

    assert verified.delivery_id == "delivery-123"
    assert verified.api_base_url == "https://gitlab.example.com/api/v4"

    with pytest.raises(HTTPException) as error:
        verify_gitlab_webhook(
            b"{}",
            legacy_token="legacy-secret",
            event_uuid="event-123",
            instance_header="https://untrusted.example",
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_webhook_payload(action="open"), "opened"),
        (_webhook_payload(action="close"), "closed"),
        (_webhook_payload(action="reopen"), "reopened"),
        (_webhook_payload(action="merge"), "closed"),
        (_webhook_payload(action="approved"), None),
    ],
)
def test_merge_request_actions_map_to_review_semantics(payload, expected):
    assert gitlab_merge_request_action(payload) == expected


def test_update_action_distinguishes_code_and_review_relevant_metadata():
    code = _webhook_payload(action="update")
    code["object_attributes"]["oldrev"] = "c" * 40
    assert gitlab_merge_request_action(code) == "synchronize"

    title = _webhook_payload(action="update")
    title["changes"] = {"title": {"previous": "old", "current": "new"}}
    assert gitlab_merge_request_action(title) == "edited"

    reviewer = _webhook_payload(action="update")
    reviewer["changes"] = {"reviewers": {"previous": [], "current": []}}
    assert gitlab_merge_request_action(reviewer) is None

    human_description = "Human-authored purpose."
    managed_description = (
        f"{human_description}\n\n"
        "<!-- diffuse-review-description:start -->\n"
        f"<!-- diffuse-review:42:{'a' * 40} -->\n"
        "## Diffuse code review\n"
        "<!-- diffuse-review-description:end -->"
    )
    diffuse_description = _webhook_payload(action="update")
    diffuse_description["object_attributes"]["description"] = managed_description
    diffuse_description["changes"] = {
        "description": {
            "previous": human_description,
            "current": managed_description,
        }
    }
    assert gitlab_merge_request_action(diffuse_description) is None

    human_edit = _webhook_payload(action="update")
    human_edit["object_attributes"]["description"] = managed_description.replace(
        human_description,
        "Changed human purpose.",
    )
    human_edit["changes"] = {
        "description": {
            "previous": managed_description,
            "current": human_edit["object_attributes"]["description"],
        }
    }
    assert gitlab_merge_request_action(human_edit) == "edited"


@pytest.mark.anyio
async def test_merge_request_enrichment_uses_authoritative_diff_identity(monkeypatch):
    verified = _verified(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_merge_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_gitlab_merge_request_event(
            _webhook_payload(),
            verified=verified,
            action="opened",
            client=client,
        )

    assert [request.url.path for request in requests] == [
        "/api/v4/projects/91/merge_requests/17"
    ]
    assert event.provider == "gitlab"
    assert event.repo_full_name == "group/subgroup/repo"
    assert event.number == 17
    assert event.base_sha == "b" * 40
    assert event.head_sha == "a" * 40
    assert event.start_sha == "b" * 40
    assert event.changed_file_count == 4
    assert event.source_project_id == 122
    assert event.labels == ("backend", "security")
    assert event.source_created_at == "2026-07-23T14:00:00+00:00"
    assert event.metadata_complete


@pytest.mark.anyio
async def test_manual_merge_request_fetch_verifies_project_and_current_state():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.raw_path == (
            b"/api/v4/projects/group%2Fsubgroup%2Frepo"
        ):
            return httpx.Response(
                200,
                json={
                    "id": 91,
                    "path_with_namespace": "group/subgroup/repo",
                    "web_url": (
                        "https://gitlab.example.com/group/subgroup/repo"
                    ),
                },
            )
        return httpx.Response(200, json=_merge_request_json())

    request = ManualReviewRequest(
        repo_full_name="group/subgroup/repo",
        number=17,
        trigger_id="mcp:test",
        requested_by="ide-agent",
        requested_at="2026-07-23T19:00:00Z",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_manual_gitlab_merge_request_event(
            request,
            delivery_id="mcp-delivery",
            scm_base_url="https://gitlab.example.com",
            api_base_url="https://gitlab.example.com/api/v4",
            client=client,
        )

    assert [value.url.raw_path for value in requests] == [
        b"/api/v4/projects/group%2Fsubgroup%2Frepo",
        b"/api/v4/projects/91/merge_requests/17",
    ]
    assert event.provider == "gitlab"
    assert event.action == "manual"
    assert event.trigger_kind == "manual"
    assert event.trigger_id == "mcp:test"
    assert event.updated_at == "2026-07-23T19:00:00+00:00"
    assert event.source_project_id == 122
    assert event.state == "open"


@pytest.mark.anyio
async def test_manual_merge_request_fetch_rejects_identity_mismatch_and_closed_state():
    request = ManualReviewRequest(
        repo_full_name="group/subgroup/repo",
        number=17,
        trigger_id="mcp:test",
        requested_by="ide-agent",
        requested_at="2026-07-23T19:00:00Z",
    )

    def mismatched_project(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 91,
                "path_with_namespace": "another/repo",
                "web_url": "https://gitlab.example.com/another/repo",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(mismatched_project)
    ) as client:
        with pytest.raises(ValueError, match="project identity"):
            await fetch_manual_gitlab_merge_request_event(
                request,
                delivery_id="mcp-mismatch",
                scm_base_url="https://gitlab.example.com",
                api_base_url="https://gitlab.example.com/api/v4",
                client=client,
            )

    def closed_merge_request(http_request: httpx.Request) -> httpx.Response:
        if "/merge_requests/" in http_request.url.path:
            return httpx.Response(
                200,
                json=_merge_request_json(
                    state="closed",
                    closed_at="2026-07-23T18:00:00.000Z",
                ),
            )
        return httpx.Response(
            200,
            json={
                "id": 91,
                "path_with_namespace": "group/subgroup/repo",
                "web_url": "https://gitlab.example.com/group/subgroup/repo",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(closed_merge_request)
    ) as client:
        with pytest.raises(ValueError, match="open merge request"):
            await fetch_manual_gitlab_merge_request_event(
                request,
                delivery_id="mcp-closed",
                scm_base_url="https://gitlab.example.com",
                api_base_url="https://gitlab.example.com/api/v4",
                client=client,
            )


@pytest.mark.anyio
async def test_merge_request_enrichment_falls_back_to_latest_diff_version(monkeypatch):
    verified = _verified(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "head_commit_sha": "a" * 40,
                        "base_commit_sha": "b" * 40,
                        "start_commit_sha": "c" * 40,
                        "real_size": "7",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=_merge_request_json(diff_refs=None, changes_count=None),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_gitlab_merge_request_event(
            _webhook_payload(),
            verified=verified,
            action="opened",
            client=client,
        )

    assert event.base_sha == "b" * 40
    assert event.head_sha == "a" * 40
    assert event.start_sha == "c" * 40
    assert event.changed_file_count == 7


@pytest.mark.anyio
async def test_merge_request_enrichment_rejects_stale_diff_refs_for_current_head(
    monkeypatch,
):
    verified = _verified(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "head_commit_sha": "a" * 40,
                        "base_commit_sha": "d" * 40,
                        "start_commit_sha": "e" * 40,
                        "real_size": "4",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=_merge_request_json(
                diff_refs={
                    "base_sha": "b" * 40,
                    "head_sha": "c" * 40,
                    "start_sha": "b" * 40,
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_gitlab_merge_request_event(
            _webhook_payload(),
            verified=verified,
            action="synchronize",
            client=client,
        )

    assert event.base_sha == "d" * 40
    assert event.head_sha == "a" * 40
    assert event.start_sha == "e" * 40


@pytest.mark.anyio
async def test_merge_request_enrichment_refuses_unprepared_diff_identity(monkeypatch):
    verified = _verified(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=_merge_request_json(diff_refs=None, changes_count=None),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitLabMetadataPendingError):
            await fetch_gitlab_merge_request_event(
                _webhook_payload(),
                verified=verified,
                action="opened",
                client=client,
            )


@pytest.mark.anyio
async def test_capped_gitlab_change_count_is_not_marked_authoritative(monkeypatch):
    verified = _verified(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "head_commit_sha": "a" * 40,
                        "base_commit_sha": "b" * 40,
                        "real_size": "1000+",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=_merge_request_json(changes_count="1000+"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        event = await fetch_gitlab_merge_request_event(
            _webhook_payload(),
            verified=verified,
            action="opened",
            client=client,
        )

    assert event.changed_file_count == 1000
    assert not event.metadata_complete


@pytest.mark.anyio
async def test_review_interaction_enriches_authorized_gitlab_diff_reply(monkeypatch):
    verified = _verified(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/members/all/" in request.url.path:
            return httpx.Response(200, json={"id": 8, "access_level": 30})
        if request.url.path.endswith("/discussions"):
            return httpx.Response(200, json=[_note_discussion()])
        return httpx.Response(200, json=_merge_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload(),
            verified=verified,
            client=client,
        )

    assert interaction.feedback is not None
    assert interaction.feedback.provider == "gitlab"
    assert interaction.feedback.root_comment_id == "202"
    assert interaction.feedback.file_path == "service/read.py"
    assert interaction.feedback.author_association == "COLLABORATOR"
    assert interaction.conversation is not None
    assert interaction.conversation.question == "why is this unsafe?"
    assert interaction.conversation.thread_id == "discussion-1"
    assert interaction.conversation.line == 12
    assert interaction.conversation.side == "RIGHT"
    assert interaction.conversation.comment_commit_sha == "a" * 40
    assert paths == [
        "/api/v4/projects/91/members/all/8",
        "/api/v4/projects/91/merge_requests/17",
        "/api/v4/projects/91/merge_requests/17/discussions",
    ]


@pytest.mark.anyio
async def test_review_interaction_enriches_authorized_top_level_manual_review(
    monkeypatch,
):
    verified = _verified(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/members/all/" in request.url.path:
            return httpx.Response(200, json={"id": 8, "access_level": 30})
        if request.url.path.endswith("/discussions"):
            return httpx.Response(200, json=[_top_level_note_discussion()])
        return httpx.Response(200, json=_merge_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload("@diffuse review this merge request"),
            verified=verified,
            client=client,
        )

    assert interaction.feedback is None
    assert interaction.conversation is None
    assert interaction.manual_requested_by == "reviewer"
    assert interaction.manual_review is not None
    assert interaction.manual_review.action == "manual"
    assert interaction.manual_review.trigger_kind == "manual"
    assert interaction.manual_review.trigger_id == "note:401"
    assert interaction.manual_review.delivery_id == "event-uuid-1"
    assert interaction.manual_review.updated_at == "2026-07-23T18:30:00+00:00"
    assert interaction.manual_review.head_sha == "a" * 40
    assert interaction.manual_review.source_project_id == 122
    assert paths == [
        "/api/v4/projects/91/members/all/8",
        "/api/v4/projects/91/merge_requests/17",
        "/api/v4/projects/91/merge_requests/17/discussions",
        "/api/v4/projects/91/merge_requests/17",
    ]


@pytest.mark.anyio
async def test_top_level_note_without_manual_command_is_ignored(monkeypatch):
    verified = _verified(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/members/all/" in request.url.path:
            return httpx.Response(200, json={"id": 8, "access_level": 30})
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[_top_level_note_discussion("ordinary discussion")],
            )
        return httpx.Response(200, json=_merge_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload("ordinary discussion"),
            verified=verified,
            client=client,
        )

    assert interaction == type(interaction)(feedback=None, conversation=None)


@pytest.mark.anyio
async def test_root_diff_note_does_not_become_a_manual_review(monkeypatch):
    verified = _verified(monkeypatch)
    diff_root = _top_level_note_discussion()
    diff_root["notes"][0]["position"] = {
        "head_sha": "a" * 40,
        "new_path": "service/read.py",
        "new_line": 12,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/members/all/" in request.url.path:
            return httpx.Response(200, json={"id": 8, "access_level": 30})
        if request.url.path.endswith("/discussions"):
            return httpx.Response(200, json=[diff_root])
        return httpx.Response(200, json=_merge_request_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload("@diffuse review this"),
            verified=verified,
            client=client,
        )

    assert interaction.manual_review is None
    assert interaction.feedback is None
    assert interaction.conversation is None


@pytest.mark.anyio
async def test_review_interaction_ignores_nonmember_and_diffuse_notes(monkeypatch):
    verified = _verified(monkeypatch)
    calls = 0

    def unauthorized(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"message": "Not found"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unauthorized)
    ) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload("context without mention"),
            verified=verified,
            client=client,
        )
    assert interaction.feedback is None
    assert interaction.conversation is None
    assert calls == 1

    calls = 0
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unauthorized)
    ) as client:
        interaction = await fetch_gitlab_review_interaction(
            _note_payload(
                "Generated reply\n<!-- diffuse-conversation:400 -->"
            ),
            verified=verified,
            client=client,
        )
    assert interaction.feedback is None
    assert interaction.conversation is None
    assert calls == 0


def test_default_branch_push_normalizes_and_branch_or_delete_is_ignored(monkeypatch):
    verified = _verified(monkeypatch)
    payload = _webhook_payload()
    payload.update(
        {
            "ref": "refs/heads/main",
            "before": "b" * 40,
            "after": "a" * 40,
            "event_created_at": "2026-07-23T15:30:00Z",
            "commits": [],
        }
    )

    event = normalize_gitlab_push_event(payload, verified=verified)

    assert event is not None
    assert event.provider == "gitlab"
    assert event.after_sha == "a" * 40

    payload["ref"] = "refs/heads/feature"
    assert normalize_gitlab_push_event(payload, verified=verified) is None
    payload["ref"] = "refs/heads/main"
    payload["after"] = "0" * 40
    assert normalize_gitlab_push_event(payload, verified=verified) is None
