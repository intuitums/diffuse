from dataclasses import replace

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
from service.github.api import normalize_manual_review_request
from service.github.review import post_github_review_failure_notice
from service.review.failure_notice import (
    TerminalReviewFailure,
    format_failure_notice,
    redact_credentials,
    terminal_review_failure,
)
from service.review.interaction import is_diffuse_generated
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


def test_terminal_failure_distinguishes_exhaustion_from_deterministic_faults():
    exhausted = terminal_review_failure(4821, retries_exhausted=True)
    deterministic = terminal_review_failure(4821, retries_exhausted=False)

    assert exhausted.error_code == "review_workflow_exhausted"
    assert "exhausting" in exhausted.summary
    assert deterministic.error_code == "review_workflow_failed"
    assert "exhausting" not in deterministic.summary


def test_failure_notice_names_the_error_code_and_job_id():
    body = format_failure_notice(terminal_review_failure(4821, retries_exhausted=True))

    # The marker is per-pull-request, deliberately without the job id, so a
    # repeated failure edits one notice instead of appending another.
    assert body.startswith("<!-- diffuse-review-failure -->")
    assert "4821" not in body.splitlines()[0]
    assert "`review_workflow_exhausted`" in body
    # The job id still has to be visible; support needs it to find the logs.
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
async def test_github_second_terminal_pass_edits_the_existing_notice(monkeypatch):
    """A later failing job must edit the one notice, not append another.

    The marker used to embed job_id, so every retry and every subsequent failing
    job posted a fresh comment onto a pull request that was already failing.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    first = terminal_review_failure(4821, retries_exhausted=True)
    second = terminal_review_failure(9999, retries_exhausted=False)
    methods: list[str] = []
    edited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": 88, "body": format_failure_notice(first)}],
            )
        edited.append(request.content.decode())
        return httpx.Response(200, json={"id": 88})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        external_id = await post_github_review_failure_notice(
            _github_event(),
            failure=second,
            client=client,
        )

    assert external_id == "88"
    assert methods == ["GET", "PATCH"]
    # The notice is refreshed to the current job, not left stale.
    assert "9999" in edited[0]
    assert "review_workflow_failed" in edited[0]


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


def _eligible_event(**overrides):
    """A normalized, fully-enriched automatic event, as the provider API yields."""
    # metadata_complete=True makes PullRequestEvent validate the enriched
    # fields, so they all have to be present and well formed.
    defaults = {
        "is_draft": False,
        "action": "opened",
        "trigger_kind": "automatic",
        "metadata_complete": True,
        "author": "octocat",
        "base_branch": "main",
        "head_branch": "feature/tenant-scope",
        "title": "Scope the query to the caller's tenant",
    }
    return replace(_github_event(), **{**defaults, **overrides})


def test_no_failure_notice_on_a_pull_request_diffuse_would_not_review(monkeypatch):
    """A draft must not be told a review it never asked for did not complete.

    A terminal failure can happen before trigger evaluation, so the notice path
    has to evaluate eligibility itself. Regression: an earlier version of this
    gate read a field name that does not exist on TriggerDecision, and a broad
    `except Exception` turned that AttributeError into fail-open -- the gate did
    nothing while its test still passed. This asserts the suppression directly.
    """
    from service.hosted import worker

    snapshot = _snapshot()
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 11)
    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(worker, "load_repository_policy", lambda *_a: snapshot)

    # review_drafts defaults off, so a draft is a deliberate exclusion.
    assert (
        worker._failure_notice_enabled("acme/api", 1, _eligible_event(is_draft=True))
        is False
    )
    # Without the event there is nothing to evaluate, so it still fails open.
    assert worker._failure_notice_enabled("acme/api", 1) is True


def test_failure_notice_still_posts_for_an_eligible_pull_request(monkeypatch):
    from service.hosted import worker

    snapshot = _snapshot()
    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 11)
    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(worker, "load_repository_policy", lambda *_a: snapshot)

    assert worker._failure_notice_enabled("acme/api", 1, _eligible_event()) is True


def test_unenriched_metadata_does_not_suppress_the_failure_notice(monkeypatch):
    """`metadata_unavailable` must still post -- it is often the failure itself.

    Diffuse could not enrich the event from the provider API, which is a common
    cause of the very failure being reported. Treating that as "not eligible"
    would silence the notice in exactly the case it exists for.
    """
    from service.hosted import worker

    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 11)
    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(worker, "load_repository_policy", lambda *_a: _snapshot())

    unenriched = replace(
        _github_event(),
        trigger_kind="automatic",
        action="opened",
        metadata_complete=False,
    )

    assert worker._failure_notice_enabled("acme/api", 1, unenriched) is True


def test_failure_notice_respects_the_repository_opt_out_before_eligibility(monkeypatch):
    from service.hosted import worker

    monkeypatch.setattr(worker, "compatible_snapshot_id", lambda *_a: 11)
    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(
        worker, "load_repository_policy", lambda *_a: _snapshot(failure_comment=False)
    )

    assert worker._failure_notice_enabled("acme/api", 1, _github_event()) is False


class _NullConn:
    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False
