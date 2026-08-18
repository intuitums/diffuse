"""PostgreSQL coverage for the pushed-revision debounce in `enqueue_review_event`.

`triggers.review_updates` defaults on, so every push to an open pull request
costs a model call. `REVIEW_UPDATE_DEBOUNCE_SECONDS` is the answer to that cost,
and it lives in the enqueue path rather than in policy: the webhook process has
no diff yet, so it cannot resolve the per-path policy the worker later does.

These assertions are about `workflow_jobs.available_at`, which only PostgreSQL
computes -- `LEAST(now() + interval, inherited)` has no in-process counterpart --
so they cannot be unit tests.
"""

import os
from contextlib import closing

import psycopg2
import pytest
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.workflow import claim_workflow_job, enqueue_review_event


# One repository per test. The claim query refuses a scope that already has a
# running job, so tests that leave one claimed would silently starve the next.
def _event(
    repository: str,
    *,
    action: str,
    head: str,
    delivery: str,
    updated_at: str,
) -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": repository,
            "number": 77,
            "web_url": f"https://github.com/{repository}/pull/77",
            "action": action,
            "head_sha": head,
            "base_sha": "0" * 40,
            "updated_at": updated_at,
            "delivery_id": delivery,
        }
    )


def _available_at(connection, job_id: int):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT available_at, revision FROM workflow_jobs WHERE id = %s",
            (job_id,),
        )
        return cursor.fetchone()


def _register(connection, repository: str) -> None:
    register_repository(
        connection,
        scm_provider="github",
        scm_base_url="https://github.com",
        full_name=repository,
        default_branch="main",
    )


def test_a_burst_of_pushes_collapses_into_one_review_of_the_final_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three pushes, one claimable job, and the window does not restart.

    The window is deliberately long enough that a restarting one would be
    measurable: if the third push were allowed to set its own `now() + 3600`, the
    deadline would move later than the first push's by the wall-clock cost of the
    two enqueues. Inheriting it instead is what stops a steady drip of commits
    from deferring the review forever -- the failure mode that would make this
    debounce indistinguishable from the off-by-default behaviour it replaces.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "3600")
    repository = "store/update-debounce-burst"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        first = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="1" * 40,
                delivery="debounce-push-1",
                updated_at="2026-07-23T17:00:00Z",
            ),
            payload_sha256="1" * 64,
        )
        second = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="2" * 40,
                delivery="debounce-push-2",
                updated_at="2026-07-23T17:00:10Z",
            ),
            payload_sha256="2" * 64,
        )
        third = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="3" * 40,
                delivery="debounce-push-3",
                updated_at="2026-07-23T17:00:20Z",
            ),
            payload_sha256="3" * 64,
        )

        assert first.job_id is not None
        assert second.job_id is not None
        assert third.job_id is not None
        assert claim_workflow_job(connection, "debounce-worker", lease_seconds=60) is None

        first_deadline, _ = _available_at(connection, first.job_id)
        third_deadline, third_revision = _available_at(connection, third.job_id)
        assert third_deadline == first_deadline
        assert third_revision == "3" * 40

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM workflow_jobs WHERE id = ANY(%s) ORDER BY id",
                ([first.job_id, second.job_id, third.job_id],),
            )
            assert [row[0] for row in cursor.fetchall()] == [
                "superseded",
                "superseded",
                "queued",
            ]

        # The window elapsing is the only thing between the survivor and a
        # worker; nothing else about the burst left it unclaimable.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_jobs SET available_at = now() WHERE id = %s",
                (third.job_id,),
            )
        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == third.job_id
        assert claimed.revision == "3" * 40


def test_an_opened_pull_request_never_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait is a re-review cost control, and there is no re-review to control.

    A first review nobody asked to defer is the one thing this must not delay:
    the whole reason `review_updates` was off by default was that Diffuse looked
    broken, and a minute of silence on `opened` would trade that for a different
    version of the same complaint.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "3600")
    repository = "store/update-debounce-opened"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        opened = enqueue_review_event(
            connection,
            _event(
                repository,
                action="opened",
                head="4" * 40,
                delivery="debounce-opened",
                updated_at="2026-07-23T18:00:00Z",
            ),
            payload_sha256="4" * 64,
        )

        assert opened.job_id is not None
        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == opened.job_id


def test_a_push_cannot_postpone_a_review_that_was_already_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Superseding a queued `opened` job inherits its deadline, which is now.

    The author who opens a pull request and immediately pushes a fixup races the
    worker's poll. Whichever side wins, the review they are waiting for must not
    slide a full window into the future because of a commit that arrived while it
    sat in the queue -- the debounce may only ever bring work forward.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "3600")
    repository = "store/update-debounce-opened-then-pushed"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        opened = enqueue_review_event(
            connection,
            _event(
                repository,
                action="opened",
                head="7" * 40,
                delivery="debounce-opened-then-pushed-1",
                updated_at="2026-07-23T20:00:00Z",
            ),
            payload_sha256="8" * 64,
        )
        pushed = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="8" * 40,
                delivery="debounce-opened-then-pushed-2",
                updated_at="2026-07-23T20:00:01Z",
            ),
            payload_sha256="9" * 64,
        )

        assert opened.job_id is not None
        assert pushed.job_id is not None
        opened_deadline, _ = _available_at(connection, opened.job_id)
        pushed_deadline, _ = _available_at(connection, pushed.job_id)
        assert pushed_deadline == opened_deadline

        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == pushed.job_id
        assert claimed.revision == "8" * 40


def test_an_edit_arriving_mid_wait_cancels_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-push decision does not join the burst; it replaces it.

    `edited`, `labeled`, `ready_for_review`, and `converted_to_draft` can each
    change the trigger decision without changing the head, and they are all
    events an operator just performed and is watching for. Inheriting the pending
    push's deadline would make a label added to hurry a review along do the
    opposite.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "3600")
    repository = "store/update-debounce-edited"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        pushed = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="5" * 40,
                delivery="debounce-then-edited-1",
                updated_at="2026-07-23T18:00:10Z",
            ),
            payload_sha256="5" * 64,
        )
        assert pushed.job_id is not None
        assert claim_workflow_job(connection, "debounce-worker", lease_seconds=60) is None

        edited = enqueue_review_event(
            connection,
            _event(
                repository,
                action="edited",
                head="5" * 40,
                delivery="debounce-then-edited-2",
                updated_at="2026-07-23T18:00:20Z",
            ),
            payload_sha256="6" * 64,
        )
        assert edited.job_id is not None
        assert edited.job_id != pushed.job_id

        pushed_deadline, _ = _available_at(connection, pushed.job_id)
        edited_deadline, _ = _available_at(connection, edited.job_id)
        assert edited_deadline < pushed_deadline

        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == edited.job_id


def test_converting_to_draft_supersedes_a_waiting_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft transition replaces the stale ready-for-review job immediately."""
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "3600")
    repository = "store/update-debounce-converted-to-draft"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        pushed = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="9" * 40,
                delivery="debounce-then-draft-1",
                updated_at="2026-07-23T21:00:00Z",
            ),
            payload_sha256="a" * 64,
        )
        draft = enqueue_review_event(
            connection,
            PullRequestEvent.from_payload(
                {
                    "provider": "github",
                    "scm_base_url": "https://github.com",
                    "api_base_url": "https://api.github.com",
                    "repo_full_name": repository,
                    "number": 77,
                    "web_url": f"https://github.com/{repository}/pull/77",
                    "action": "converted_to_draft",
                    "head_sha": "9" * 40,
                    "base_sha": "0" * 40,
                    "updated_at": "2026-07-23T21:00:01Z",
                    "delivery_id": "debounce-then-draft-2",
                    "author": "octocat",
                    "base_branch": "main",
                    "head_branch": "feature/draft",
                    "is_draft": True,
                    "labels": [],
                    "title": "Pause this review",
                    "description": "",
                    "trigger_kind": "automatic",
                    "trigger_id": "",
                    "metadata_complete": True,
                    "changed_file_count": 1,
                }
            ),
            payload_sha256="b" * 64,
        )

        assert pushed.job_id is not None
        assert draft.job_id is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM workflow_jobs WHERE id = %s",
                (pushed.job_id,),
            )
            assert cursor.fetchone()[0] == "superseded"

        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == draft.job_id
        claimed_event = PullRequestEvent.from_payload(claimed.payload)
        assert claimed_event.action == "converted_to_draft"
        assert claimed_event.is_draft


def test_a_zero_window_reviews_every_push_immediately() -> None:
    """`REVIEW_UPDATE_DEBOUNCE_SECONDS=0` is the documented opt-out.

    The autouse fixture in `conftest.py` already sets it, which is what keeps the
    store tests' `synchronize` scaffolding claimable; this asserts the behaviour
    those tests depend on rather than leaving it implied.
    """
    repository = "store/update-debounce-disabled"

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        _register(connection, repository)

        pushed = enqueue_review_event(
            connection,
            _event(
                repository,
                action="synchronize",
                head="6" * 40,
                delivery="debounce-disabled",
                updated_at="2026-07-23T19:00:00Z",
            ),
            payload_sha256="7" * 64,
        )

        assert pushed.job_id is not None
        claimed = claim_workflow_job(connection, "debounce-worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.id == pushed.job_id
