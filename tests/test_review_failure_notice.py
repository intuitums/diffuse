import httpx
import pytest

from repository_policy.models import (
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
    TriggerSettingsPatch,
)
from repository_policy.resolve import (
    repository_failure_comment_enabled,
    resolve_review_policy,
)
from service.github import normalize_manual_review_request
from service.github_review import post_github_review_failure_notice
from service.gitlab import fetch_gitlab_review_interaction, verify_gitlab_webhook
from service.gitlab_review import post_gitlab_review_failure_notice
from service.review_failure_notice import (
    TerminalReviewFailure,
    format_failure_notice,
    redact_credentials,
    terminal_review_failure,
)
from service.review_interaction import is_diffuse_generated
from service.scm import PullRequestEvent


def _github_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "owner/repo",
            "number": 7,
            "web_url": "https://github.com/owner/repo/pull/7",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
        }
    )


def _gitlab_event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "owner/repo",
            "number": 7,
            "web_url": "https://gitlab.example.com/owner/repo/-/merge_requests/7",
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-7",
        }
    )


def test_terminal_failure_distinguishes_exhaustion_from_deterministic_faults():
    exhausted = terminal_review_failure(4821, retries_exhausted=True)
    deterministic = terminal_review_failure(4821, retries_exhausted=False)

    assert exhausted.error_code == "review_workflow_exhausted"
    assert "exhausting" in exhausted.summary
    assert deterministic.error_code == "review_workflow_failed"
    assert "exhausting" not in deterministic.summary


def test_failure_notice_names_the_error_code_and_job_id():
    body = format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))

    assert body.startswith("<!-- diffuse-review-failure:4821 -->")
    assert "`review_workflow_exhausted`" in body
    assert "`4821`" in body
    assert body.count("\n\n") == 4


def test_failure_notice_is_recognized_as_diffuse_generated():
    """The marker is what stops Diffuse ingesting its own notice as feedback."""
    body = format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))

    assert is_diffuse_generated(body) is True


def test_failure_notice_never_carries_a_manual_review_trigger():
    body = format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))

    assert "@diffuse" not in body


def test_github_ignores_its_own_failure_notice_as_a_manual_trigger():
    """A Diffuse-authored comment must not re-queue the review it reports on."""
    body = (
        format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))
        + "\n@diffuse review"
    )
    payload = {
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7, "pull_request": {"url": "https://api/pulls/7"}},
        "comment": {
            "id": 55,
            "body": body,
            "created_at": "2026-07-23T15:30:00Z",
            "user": {"login": "maintainer", "type": "User"},
            "author_association": "OWNER",
        },
    }

    assert normalize_manual_review_request(payload) is None


@pytest.mark.anyio
async def test_gitlab_ignores_its_own_failure_notice(monkeypatch):
    """GitLab note ingestion drops the notice before any enrichment call."""
    monkeypatch.setenv("GITLAB_WEB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_API_URL", "https://gitlab.example.com/api/v4")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "legacy-secret")
    verified = verify_gitlab_webhook(
        b"{}",
        legacy_token="legacy-secret",
        event_uuid="event-uuid-1",
        instance_header="https://gitlab.example.com",
    )
    body = (
        format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))
        + "\n@diffuse review"
    )
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 8, "username": "reviewer", "name": "Reviewer"},
        "project_id": 91,
        "project": {
            "id": 91,
            "path_with_namespace": "group/repo",
            "web_url": "https://gitlab.example.com/group/repo",
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
        "merge_request": {"iid": 17, "state": "opened", "target_project_id": 91},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("GitLab must not be called for a Diffuse-authored note")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interaction = await fetch_gitlab_review_interaction(
            payload,
            verified=verified,
            client=client,
        )

    assert interaction.feedback is None
    assert interaction.conversation is None


def test_credential_shaped_text_is_redacted():
    redacted = redact_credentials(
        "postgres://diffuse:hunter2@db:5432/diffuse "
        "ghp_abcdefghijklmnopqrstuvwxyz012345 "
        "glpat-abcdefghijklmnopqrst "
        "Authorization: Bearer abc.def.ghi"
    )

    assert "hunter2" not in redacted
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "glpat-abcdefghijklmnopqrst" not in redacted
    assert "abc.def.ghi" not in redacted


def test_invalid_failure_codes_are_rejected():
    with pytest.raises(ValueError):
        TerminalReviewFailure(job_id=1, error_code="Not A Slug", summary="x")
    with pytest.raises(ValueError):
        TerminalReviewFailure(job_id=0, error_code="ok", summary="x")


@pytest.mark.anyio
async def test_github_posts_exactly_one_failure_notice(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    failure = terminal_review_failure(4821, retries_exhausted=True)
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "body": "unrelated"}])
        posted.append(request.content.decode())
        return httpx.Response(201, json={"id": 88})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        external_id = await post_github_review_failure_notice(
            _github_event(),
            failure=failure,
            client=client,
        )

    assert external_id == "88"
    assert len(posted) == 1
    assert failure.marker in posted[0]


@pytest.mark.anyio
async def test_github_second_terminal_pass_reuses_the_existing_notice(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    failure = terminal_review_failure(4821, retries_exhausted=True)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[{"id": 88, "body": format_failure_notice(failure)}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        external_id = await post_github_review_failure_notice(
            _github_event(),
            failure=failure,
            client=client,
        )

    assert external_id == "88"
    assert methods == ["GET"]


@pytest.mark.anyio
async def test_gitlab_posts_exactly_one_failure_notice(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    failure = terminal_review_failure(4821, retries_exhausted=False)
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "body": "unrelated"}])
        posted.append(request.content.decode())
        return httpx.Response(201, json={"id": 88})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        external_id = await post_gitlab_review_failure_notice(
            _gitlab_event(),
            failure=failure,
            client=client,
        )

    assert external_id == "88"
    assert len(posted) == 1
    assert failure.marker in posted[0]


@pytest.mark.anyio
async def test_gitlab_second_terminal_pass_reuses_the_existing_notice(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    failure = terminal_review_failure(4821, retries_exhausted=False)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[{"id": 88, "body": format_failure_notice(failure)}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        external_id = await post_gitlab_review_failure_notice(
            _gitlab_event(),
            failure=failure,
            client=client,
        )

    assert external_id == "88"
    assert methods == ["GET"]


@pytest.mark.anyio
async def test_failure_notice_publishers_reject_the_wrong_provider():
    failure = terminal_review_failure(4821, retries_exhausted=True)

    with pytest.raises(ValueError):
        await post_github_review_failure_notice(_gitlab_event(), failure=failure)
    with pytest.raises(ValueError):
        await post_gitlab_review_failure_notice(_github_event(), failure=failure)


def _snapshot(**trigger_overrides) -> RepositoryPolicySnapshot:
    return RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="",
                source_path=".diffuse/config.json",
                config=RepositoryConfig(
                    version=1,
                    triggers=TriggerSettingsPatch(**trigger_overrides),
                ),
            ),
        )
    )


def test_failure_comment_defaults_on_and_is_repository_configurable():
    assert repository_failure_comment_enabled(RepositoryPolicySnapshot()) is True
    assert repository_failure_comment_enabled(_snapshot()) is True
    assert repository_failure_comment_enabled(_snapshot(failure_comment=False)) is False
    assert repository_failure_comment_enabled(_snapshot(failure_comment=True)) is True


def test_nested_scopes_cannot_disable_the_repository_failure_notice():
    """A review can die before its diff is read, so only the root layer counts."""
    snapshot = RepositoryPolicySnapshot(
        layers=(
            PolicyLayer(
                directory_path="src",
                source_path="src/.diffuse/config.json",
                config=RepositoryConfig(
                    version=1,
                    triggers=TriggerSettingsPatch(failure_comment=False),
                ),
            ),
        )
    )

    assert repository_failure_comment_enabled(snapshot) is True
    assert resolve_review_policy(snapshot, {"src/app.py"}).triggers.failure_comment is False
