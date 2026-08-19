"""PostgreSQL coverage for `diffuse.database.finding` and finding lineage.

Recovered from `tests/integration/test_workflow_postgres.py`, which was named
for the workflow module but held the only durability assertions for finding
lineage and its GitHub threads. `packages/server/tests/test_finding_lineage.py` covers
the pure classifier only -- nothing else drives `record_finding_threads`,
`begin_thread_operations` or `load_review_continuity` against real SQL.

`test_finding_lineage_addresses_and_reopens_one_durable_thread` is W1.2's
done-when, verbatim: "a finding fixed in a follow-up commit resolves its thread;
a regression reopens it. Both idempotent across reruns."

`begin_review_run` is still keyed by `workflow_job_id`, so `_claimed_job` and
`_finish_job` are unavoidable scaffolding today; nothing here asserts on queue
behaviour. When W1.1 re-keys a review run to the CLI invocation, only those two
helpers change.
"""

import os
from contextlib import closing

import psycopg2
from diffuse.database.finding import (
    PublishedFindingComment,
    PublishedThreadOperation,
    begin_thread_operations,
    latest_published_review_head,
    load_review_continuity,
    mark_thread_operation_published,
    record_finding_threads,
)
from diffuse.database.review_store import (
    begin_publication,
    begin_review_run,
    mark_publication_published,
    mark_review_superseded,
    persist_review_report,
)
from diffuse.repository.registry import register_repository
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.workflow import (
    claim_workflow_job,
    complete_workflow_job,
    enqueue_review_event,
)
from diffuse_protocol.review import (
    Category,
    ReviewFinding,
    ReviewReport,
    Severity,
)


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the store schema still requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def _finish_job(connection, job_id: int, worker_id: str) -> None:
    """Scaffolding only -- releases the job so the next event can be claimed."""
    assert complete_workflow_job(connection, job_id, worker_id)


def test_finding_lineage_addresses_and_reopens_one_durable_thread():
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repo = "store/finding-lineage"

    def event(
        *,
        head: str,
        delivery: str,
        updated_at: str,
    ) -> PullRequestEvent:
        return PullRequestEvent.from_payload(
            {
                "provider": "github",
                "scm_base_url": "https://github.com",
                "api_base_url": "https://api.github.com",
                "repo_full_name": repo,
                "number": 44,
                "web_url": f"https://github.com/{repo}/pull/44",
                "action": "synchronize",
                "head_sha": head,
                "base_sha": "0" * 40,
                "updated_at": updated_at,
                "delivery_id": delivery,
            }
        )

    def finding(fingerprint: str, line: int) -> ReviewFinding:
        return ReviewFinding(
            fingerprint=fingerprint,
            title="Validate the tenant boundary",
            body="The changed handler forwards an unscoped tenant ID.",
            severity=Severity.HIGH,
            category=Category.SECURITY,
            confidence=0.92,
            file_path="service/api.py",
            line=line,
            side="RIGHT",
            evidence="The forwarded value is accepted directly from the request.",
            suggested_fix="Check tenant ownership before forwarding the value.",
        )

    def report(findings: list[ReviewFinding]) -> ReviewReport:
        return ReviewReport(
            summary="Review continuity integration fixture.",
            risk_score=7 if findings else 0,
            findings=findings,
            diff_file_count=1,
            reviewed_file_count=1,
            context_chunk_count=0,
            prompt_tokens=10,
            completion_tokens=2,
        )

    first_event = event(
        head="1" * 40,
        delivery="lineage-1",
        updated_at="2026-07-23T19:00:00Z",
    )
    second_event = event(
        head="2" * 40,
        delivery="lineage-2",
        updated_at="2026-07-23T19:01:00Z",
    )
    third_event = event(
        head="3" * 40,
        delivery="lineage-3",
        updated_at="2026-07-23T19:02:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=repo,
            default_branch="main",
        )

        first_job = _claimed_job(
            connection,
            first_event,
            payload_sha256="1" * 64,
            worker_id="lineage-worker-1",
        )
        first_run = begin_review_run(
            connection,
            workflow_job_id=first_job.id,
            repository_id=repository.id,
            pull_request_id=first_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=first_event.base_sha,
            head_sha=first_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="1" * 64,
        )
        first_finding = finding("a" * 64, 20)
        persist_review_report(
            connection,
            first_run.id,
            report([first_finding]),
        )
        first_continuity = load_review_continuity(connection, first_run.id)
        assert first_continuity.new_fingerprints == (first_finding.fingerprint,)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM finding_lineages WHERE pull_request_id = %s",
                (first_job.pull_request_id,),
            )
            assert cursor.fetchone()[0] == "pending"
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider="github",
            comments=(
                PublishedFindingComment(
                    fingerprint=first_finding.fingerprint,
                    external_id="501",
                    external_node_id="PRRC_501",
                    external_url="https://example/comment/501",
                ),
            ),
        )
        first_publication = begin_publication(
            connection,
            first_run.id,
            scm_provider="github",
        )
        assert first_publication.review_number == 1
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="review-1",
            external_url="https://example/review/1",
        )
        _finish_job(connection, first_job.id, "lineage-worker-1")

        second_job = _claimed_job(
            connection,
            second_event,
            payload_sha256="2" * 64,
            worker_id="lineage-worker-2",
        )
        second_run = begin_review_run(
            connection,
            workflow_job_id=second_job.id,
            repository_id=repository.id,
            pull_request_id=second_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=second_event.base_sha,
            head_sha=second_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="2" * 64,
        )
        assert (
            latest_published_review_head(
                connection,
                pull_request_id=second_job.pull_request_id,
                before_review_run_id=second_run.id,
            )
            == first_event.head_sha
        )
        persist_review_report(
            connection,
            second_run.id,
            report([]),
            touched_paths=frozenset({"service/api.py"}),
        )
        second_continuity = load_review_continuity(connection, second_run.id)
        assert [item.title for item in second_continuity.addressed] == [
            first_finding.title
        ]
        assert second_continuity.open_findings == ()
        second_publication = begin_publication(
            connection,
            second_run.id,
            scm_provider="github",
        )
        assert second_publication.review_number == 2
        mark_publication_published(
            connection,
            second_publication.id,
            external_id="review-2",
            external_url="https://example/review/2",
        )
        address_operations = begin_thread_operations(
            connection,
            review_run_id=second_run.id,
        )
        assert len(address_operations) == 1
        assert address_operations[0].kind == "address"
        mark_thread_operation_published(
            connection,
            address_operations[0].id,
            result=PublishedThreadOperation(
                external_reply_id="601",
                external_reply_url="https://example/comment/601",
                thread_node_id="PRRT_501",
            ),
        )
        assert (
            begin_thread_operations(
                connection,
                review_run_id=second_run.id,
            )
            == ()
        )
        _finish_job(connection, second_job.id, "lineage-worker-2")

        third_job = _claimed_job(
            connection,
            third_event,
            payload_sha256="3" * 64,
            worker_id="lineage-worker-3",
        )
        third_run = begin_review_run(
            connection,
            workflow_job_id=third_job.id,
            repository_id=repository.id,
            pull_request_id=third_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=third_event.base_sha,
            head_sha=third_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="3" * 64,
        )
        reopened_finding = finding("b" * 64, 27)
        persist_review_report(
            connection,
            third_run.id,
            report([reopened_finding]),
            touched_paths=frozenset({"service/api.py"}),
        )
        third_continuity = load_review_continuity(connection, third_run.id)
        assert third_continuity.reopened_fingerprints == (
            reopened_finding.fingerprint,
        )
        third_publication = begin_publication(
            connection,
            third_run.id,
            scm_provider="github",
        )
        assert third_publication.review_number == 3
        mark_publication_published(
            connection,
            third_publication.id,
            external_id="review-3",
            external_url="https://example/review/3",
        )
        reopen_operations = begin_thread_operations(
            connection,
            review_run_id=third_run.id,
        )
        assert len(reopen_operations) == 1
        assert reopen_operations[0].kind == "reopen"
        mark_thread_operation_published(
            connection,
            reopen_operations[0].id,
            result=PublishedThreadOperation(
                external_reply_id="602",
                external_reply_url="https://example/comment/602",
                thread_node_id="PRRT_501",
            ),
        )
        _finish_job(connection, third_job.id, "lineage-worker-3")

        fourth_event = event(
            head="4" * 40,
            delivery="lineage-4",
            updated_at="2026-07-23T19:03:00Z",
        )
        fourth_job = _claimed_job(
            connection,
            fourth_event,
            payload_sha256="4" * 64,
            worker_id="lineage-worker-4",
        )
        fourth_run = begin_review_run(
            connection,
            workflow_job_id=fourth_job.id,
            repository_id=repository.id,
            pull_request_id=fourth_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=fourth_event.base_sha,
            head_sha=fourth_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="4" * 64,
        )
        abandoned_finding = finding("c" * 64, 5).model_copy(
            update={
                "file_path": "service/new.py",
                "title": "Remove the abandoned path",
            }
        )
        persist_review_report(
            connection,
            fourth_run.id,
            report([abandoned_finding]),
            touched_paths=frozenset({"service/new.py"}),
        )
        assert mark_review_superseded(
            connection,
            fourth_run.id,
            worker_id="lineage-worker-4",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, first_seen_review_run_id, last_seen_review_run_id
                FROM finding_lineages
                WHERE pull_request_id = %s
                """,
                (third_job.pull_request_id,),
            )
            lineage_state = cursor.fetchone()
            cursor.execute(
                """
                SELECT transition
                FROM finding_lineage_events
                ORDER BY id
                """
            )
            transitions = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT status, thread_node_id FROM finding_threads"
            )
            thread_state = cursor.fetchone()
            cursor.execute(
                """
                SELECT operation_kind, status, attempt_count
                FROM finding_thread_operations
                ORDER BY id
                """
            )
            operation_states = cursor.fetchall()
            cursor.execute(
                """
                SELECT count(*)
                FROM finding_lineages
                WHERE status = 'pending'
                """
            )
            pending_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT signal_kind, suppression_protected
                FROM review_feedback_events
                WHERE source_kind = 'commit_outcome'
                ORDER BY id
                """
            )
            outcome_signals = cursor.fetchall()
        connection.rollback()

    assert lineage_state == ("active", first_run.id, third_run.id)
    assert transitions == ["new", "addressed", "reopened"]
    assert thread_state == ("active", "PRRT_501")
    assert operation_states == [
        ("address", "published", 1),
        ("reopen", "published", 1),
    ]
    assert outcome_signals == [
        ("addressed", True),
        ("reopened", True),
    ]
    assert pending_count == 0


def _unanchored_lineage_fixture_finding(
    fingerprint: str,
    *,
    title: str,
    path: str,
    line: int,
) -> ReviewFinding:
    return ReviewFinding(
        fingerprint=fingerprint,
        title=title,
        body=f"The changed handler in {path} accepts an unscoped tenant ID.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.91,
        file_path=path,
        line=line,
        side="RIGHT",
        evidence=f"Line {line} forwards the request value without a check.",
        suggested_fix="Check tenant ownership before forwarding the value.",
    )


def _unanchored_lineage_fixture_report(findings: list[ReviewFinding]) -> ReviewReport:
    return ReviewReport(
        summary="Inline attach regression fixture.",
        risk_score=7 if findings else 0,
        findings=findings,
        diff_file_count=2,
        reviewed_file_count=2,
        context_chunk_count=0,
        prompt_tokens=10,
        completion_tokens=2,
    )


def _threadless_active_lineage_count(cursor, pull_request_id: int) -> int:
    cursor.execute(
        """
        SELECT count(*)
        FROM finding_lineages AS lineage
        WHERE lineage.pull_request_id = %s
          AND lineage.status = 'active'
          AND NOT EXISTS (
              SELECT 1
              FROM finding_threads AS thread
              WHERE thread.lineage_id = lineage.id
          )
        """,
        (pull_request_id,),
    )
    return int(cursor.fetchone()[0])


def _assert_failed_inline_attach_never_activates_threadless_lineage(
    *,
    provider: str,
    repo: str,
    scm_base_url: str,
    api_base_url: str,
    web_url: str,
    number: int,
    attached_root_comment_id: str,
    retried_root_comment_id: str,
) -> None:
    """One partially attached publication must not strand a `new` lineage open."""
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]

    def event(*, head: str, delivery: str, updated_at: str) -> PullRequestEvent:
        return PullRequestEvent.from_payload(
            {
                "provider": provider,
                "scm_base_url": scm_base_url,
                "api_base_url": api_base_url,
                "repo_full_name": repo,
                "number": number,
                "web_url": web_url,
                "action": "synchronize",
                "head_sha": head,
                "base_sha": "0" * 40,
                "updated_at": updated_at,
                "delivery_id": delivery,
            }
        )

    attached = _unanchored_lineage_fixture_finding(
        "a" * 64,
        title="Validate the tenant boundary",
        path="service/attached.py",
        line=12,
    )
    unattached = _unanchored_lineage_fixture_finding(
        "b" * 64,
        title="Reject the unscoped identifier",
        path="service/unattached.py",
        line=44,
    )

    first_event = event(
        head="1" * 40,
        delivery=f"unanchored-{provider}-1",
        updated_at="2026-07-24T09:00:00Z",
    )
    second_event = event(
        head="2" * 40,
        delivery=f"unanchored-{provider}-2",
        updated_at="2026-07-24T09:01:00Z",
    )

    with closing(psycopg2.connect(database_url)) as connection:
        repository = register_repository(
            connection,
            scm_provider=provider,
            scm_base_url=scm_base_url,
            full_name=repo,
            default_branch="main",
        )
        first_job = _claimed_job(
            connection,
            first_event,
            payload_sha256="1" * 64,
            worker_id=f"unanchored-{provider}-worker-1",
        )
        first_run = begin_review_run(
            connection,
            workflow_job_id=first_job.id,
            repository_id=repository.id,
            pull_request_id=first_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=first_event.base_sha,
            head_sha=first_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="1" * 64,
        )
        persist_review_report(
            connection,
            first_run.id,
            _unanchored_lineage_fixture_report([attached, unattached]),
        )
        first_continuity = load_review_continuity(connection, first_run.id)
        assert sorted(first_continuity.new_fingerprints) == sorted(
            (attached.fingerprint, unattached.fingerprint)
        )

        # The provider attached one inline root comment and rejected the other,
        # so the publication falls back to summary text for the rejected finding.
        attached_comment = PublishedFindingComment(
            fingerprint=attached.fingerprint,
            external_id=attached_root_comment_id,
            external_node_id=None,
            external_url=f"{web_url}#note_{attached_root_comment_id}",
            thread_id=None,
        )
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider=provider,
            comments=(attached_comment,),
        )
        first_publication = begin_publication(
            connection,
            first_run.id,
            scm_provider=provider,
        )
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="published-review-1",
            external_url=f"{web_url}#review-1",
            unanchored_fingerprints=frozenset({unattached.fingerprint}),
        )

        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                first_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT
                    finding.fingerprint,
                    lineage.status,
                    event.applied_at IS NOT NULL AS applied
                FROM finding_lineage_events AS event
                JOIN finding_lineages AS lineage ON lineage.id = event.lineage_id
                JOIN review_findings AS finding ON finding.id = event.finding_id
                WHERE event.review_run_id = %s
                ORDER BY finding.fingerprint
                """,
                (first_run.id,),
            )
            assert cursor.fetchall() == [
                (attached.fingerprint, "active", True),
                (unattached.fingerprint, "pending", False),
            ]

        # Re-entrancy: replaying the same publication must not duplicate the
        # anchor, activate the withheld lineage, or change any durable state.
        record_finding_threads(
            connection,
            review_run_id=first_run.id,
            scm_provider=provider,
            comments=(attached_comment,),
        )
        mark_publication_published(
            connection,
            first_publication.id,
            external_id="published-review-1",
            external_url=f"{web_url}#review-1",
            unanchored_fingerprints=frozenset({unattached.fingerprint}),
        )
        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                first_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT root_comment_id)
                FROM finding_threads AS thread
                JOIN finding_lineages AS lineage ON lineage.id = thread.lineage_id
                WHERE lineage.pull_request_id = %s
                """,
                (first_job.pull_request_id,),
            )
            assert cursor.fetchone() == (1, 1)
        _finish_job(connection, first_job.id, f"unanchored-{provider}-worker-1")

        # The next review re-derives the withheld finding as `new`, so the
        # inline attach is retried instead of being lost forever.
        second_job = _claimed_job(
            connection,
            second_event,
            payload_sha256="2" * 64,
            worker_id=f"unanchored-{provider}-worker-2",
        )
        second_run = begin_review_run(
            connection,
            workflow_job_id=second_job.id,
            repository_id=repository.id,
            pull_request_id=second_job.pull_request_id,
            index_snapshot_id=None,
            base_sha=second_event.base_sha,
            head_sha=second_event.head_sha,
            model="openai/test-review-model",
            prompt_version="lineage-v1",
            context_fingerprint="2" * 64,
        )
        persist_review_report(
            connection,
            second_run.id,
            _unanchored_lineage_fixture_report([attached, unattached]),
            touched_paths=frozenset({attached.file_path, unattached.file_path}),
        )
        second_continuity = load_review_continuity(connection, second_run.id)
        assert second_continuity.new_fingerprints == (unattached.fingerprint,)
        assert second_continuity.persistent_fingerprints == (attached.fingerprint,)
        assert unattached.fingerprint in second_continuity.inline_fingerprints

        record_finding_threads(
            connection,
            review_run_id=second_run.id,
            scm_provider=provider,
            comments=(
                PublishedFindingComment(
                    fingerprint=unattached.fingerprint,
                    external_id=retried_root_comment_id,
                    external_node_id=None,
                    external_url=f"{web_url}#note_{retried_root_comment_id}",
                    thread_id=None,
                ),
            ),
        )
        second_publication = begin_publication(
            connection,
            second_run.id,
            scm_provider=provider,
        )
        mark_publication_published(
            connection,
            second_publication.id,
            external_id="published-review-2",
            external_url=f"{web_url}#review-2",
        )

        with connection.cursor() as cursor:
            assert _threadless_active_lineage_count(
                cursor,
                second_job.pull_request_id,
            ) == 0
            cursor.execute(
                """
                SELECT lineage.status, thread.root_comment_id
                FROM finding_lineages AS lineage
                JOIN finding_threads AS thread ON thread.lineage_id = lineage.id
                WHERE lineage.pull_request_id = %s
                ORDER BY thread.root_comment_id
                """,
                (second_job.pull_request_id,),
            )
            anchored = cursor.fetchall()
            cursor.execute(
                "SELECT count(*) FROM finding_lineages WHERE pull_request_id = %s",
                (second_job.pull_request_id,),
            )
            lineage_count = int(cursor.fetchone()[0])
        connection.rollback()

    # The withheld lineage was reused rather than duplicated by the retry.
    assert lineage_count == 2
    assert anchored == [
        ("active", attached_root_comment_id),
        ("active", retried_root_comment_id),
    ]


def test_github_failed_inline_attach_never_activates_a_threadless_lineage():
    _assert_failed_inline_attach_never_activates_threadless_lineage(
        provider="github",
        repo="store/unanchored-github",
        scm_base_url="https://github.com",
        api_base_url="https://api.github.com",
        web_url="https://github.com/store/unanchored-github/pull/71",
        number=71,
        attached_root_comment_id="710",
        retried_root_comment_id="711",
    )

