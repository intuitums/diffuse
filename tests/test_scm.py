from datetime import UTC, datetime

import httpx
import pytest

from service.scm import (
    MAX_RATE_LIMIT_DELAY_SECONDS,
    MIN_RATE_LIMIT_DELAY_SECONDS,
    PLAINTEXT_ORIGIN_VARIABLE,
    FeedbackSyncEvent,
    ProviderRateLimitError,
    PullRequestEvent,
    PushEvent,
    ReviewFeedbackCommentEvent,
    normalize_base_url,
    normalize_timestamp,
    raise_for_provider_status,
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


def test_secondary_rate_limit_carries_the_retry_after_instant():
    """A 429 must park the job past Retry-After, not burn a retry attempt."""
    response = httpx.Response(429, headers={"Retry-After": "900"})

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert 890 <= delay <= 900
    assert error.value.provider == "github"


def test_primary_rate_limit_uses_the_reset_epoch():
    reset_at = datetime.now(tz=UTC).timestamp() + 1800
    response = httpx.Response(
        403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(reset_at)),
        },
    )

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    assert 1700 <= (error.value.retry_at - datetime.now(tz=UTC)).total_seconds() <= 1800


def test_gitlab_reset_header_is_honored_and_bounded():
    response = httpx.Response(
        429,
        headers={"RateLimit-Reset": str(int(datetime.now(tz=UTC).timestamp() + 90_000))},
    )

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="gitlab")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert delay <= MAX_RATE_LIMIT_DELAY_SECONDS


def test_permission_denial_is_not_mistaken_for_a_rate_limit():
    """A plain 403 has no remaining counter and must stay a hard failure."""
    response = httpx.Response(403, request=httpx.Request("GET", "https://example/api"))

    with pytest.raises(httpx.HTTPStatusError):
        raise_for_provider_status(response, provider="github")


def test_zero_retry_after_still_parks_the_job_for_a_real_interval():
    """A reset of "now" is a hot loop, not a retry.

    The job is re-queued with its attempt refunded, so nothing bounds how many
    times it comes back; flooring the delay is the only thing that stops it
    from hammering the API that is already throttling us while it holds its
    scope key against every job queued behind it.
    """
    response = httpx.Response(429, headers={"Retry-After": "0"})

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert delay >= MIN_RATE_LIMIT_DELAY_SECONDS - 1
    assert MIN_RATE_LIMIT_DELAY_SECONDS >= 5


def test_a_reset_epoch_already_past_is_floored_not_treated_as_now():
    """A worker clock running ahead of the provider makes the reset look past."""
    stale = int(datetime.now(tz=UTC).timestamp()) - 300
    response = httpx.Response(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(stale)},
    )

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert delay >= MIN_RATE_LIMIT_DELAY_SECONDS - 1


def test_ietf_reset_header_is_read_as_delta_seconds_not_an_epoch():
    """`RateLimit-Reset: 120` means two minutes, not 1970.

    Read as an epoch it lands 56 years in the past, which collapses to an
    immediate retry — the same hot loop, reached by a header the IETF draft
    defines the other way round from GitHub's vendor-prefixed one.
    """
    response = httpx.Response(429, headers={"RateLimit-Reset": "120"})

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="gitlab")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert 110 <= delay <= 120


def test_secondary_rate_limit_with_untouched_primary_quota_is_recognized():
    """GitHub's content-creation limit is the one Diffuse actually trips.

    It answers 403 with Retry-After while the primary quota still reads 4999,
    so a remaining-counter test misses it entirely: the call falls through as
    a generic HTTP error, is classified retryable, burns every attempt inside
    the limit window and finishes as a red X on the pull request.
    """
    response = httpx.Response(
        403,
        headers={"Retry-After": "120", "X-RateLimit-Remaining": "4999"},
        request=httpx.Request("POST", "https://api.github.com/reviews"),
    )

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    delay = (error.value.retry_at - datetime.now(tz=UTC)).total_seconds()
    assert 110 <= delay <= 120


def test_secondary_rate_limit_reported_as_429_without_quota_headers():
    """The same limit is also served as a 429 with no quota headers at all."""
    response = httpx.Response(429, headers={"Retry-After": "60"})

    with pytest.raises(ProviderRateLimitError) as error:
        raise_for_provider_status(response, provider="github")

    assert 50 <= (error.value.retry_at - datetime.now(tz=UTC)).total_seconds() <= 60


def test_a_denial_with_an_unparseable_retry_after_stays_a_hard_failure():
    """Only a Retry-After that actually parses may reclassify a 403."""
    response = httpx.Response(
        403,
        headers={"Retry-After": "soon", "X-RateLimit-Remaining": "4999"},
        request=httpx.Request("GET", "https://example/api"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        raise_for_provider_status(response, provider="github")
