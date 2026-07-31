"""PostgreSQL coverage for `service/review_store.py`.

These assertions used to live in `tests/integration/test_workflow_postgres.py`,
which was named for `service/workflow.py` but was in fact the only durability
suite for seven persistence modules the rebuild keeps. They are recovered here,
per store, with the queue and worker assertions dropped.

`begin_review_run` is still keyed by `workflow_job_id`, so obtaining a
`workflow_jobs` row is unavoidable scaffolding today. It is deliberately kept to
`_claimed_job` below and asserted on nowhere: when W1.1 re-keys a review run to
the CLI invocation, only that helper changes.
"""

import os
from contextlib import closing

import psycopg2

from indexer.store import begin_index_snapshot
from retriever.context_models import RepositoryContextSnapshot
from service.repositories import register_repository
from service.review_models import (
    Category,
    ReviewDiagram,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
    Severity,
)
from service.review_store import (
    begin_publication,
    begin_review_run,
    load_review_report,
    mark_publication_failed,
    mark_publication_published,
    persist_review_report,
)
from service.scm import PullRequestEvent
from service.workflow import claim_workflow_job, enqueue_review_event


def _claimed_job(connection, event, *, payload_sha256: str, worker_id: str):
    """Scaffolding only -- the `workflow_jobs` row the store schema still requires."""
    queued = enqueue_review_event(connection, event, payload_sha256=payload_sha256)
    job = claim_workflow_job(connection, worker_id, lease_seconds=60)
    assert job is not None and job.id == queued.job_id
    assert job.pull_request_id is not None
    return job


def test_native_review_report_and_publication_are_durable_and_idempotent():
    """Round-trip every optional field of a report, then retry its publication.

    This is the repo's only `load_review_report(...) == report` assertion, and
    the fixture deliberately carries every optional field -- diagram, all four
    collapsible/default-open pairs, the footer flag and a preventative security
    classification -- because `test_review_diagram.py` and
    `test_review_description.py` cover rendering and never SQL serialisation. A
    port that silently drops a column fails here or nowhere.
    """
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "store/native-review",
            "number": 23,
            "web_url": "https://github.com/store/native-review/pull/23",
            "action": "opened",
            "head_sha": "8" * 40,
            "base_sha": "7" * 40,
            "updated_at": "2026-07-23T17:00:00Z",
            "delivery_id": "native-review-delivery",
        }
    )
    finding = ReviewFinding(
        fingerprint="f" * 64,
        title="Validate the new trust boundary",
        body="The changed line accepts untrusted data without validating it.",
        severity=Severity.HIGH,
        category=Category.SECURITY,
        confidence=0.91,
        file_path="service/api.py",
        line=42,
        side="RIGHT",
        evidence="The new call directly forwards user-controlled input.",
        suggested_fix="Validate and normalize the value before forwarding it.",
    )
    preventative_finding = ReviewFinding(
        fingerprint="e" * 64,
        title="Constrain the future redirect target",
        body=(
            "The new helper accepts an unrestricted target that is not currently "
            "reachable from untrusted input."
        ),
        severity=Severity.MEDIUM,
        category=Category.SECURITY,
        security_classification=SecurityClassification.PREVENTATIVE,
        confidence=0.93,
        file_path="service/redirects.py",
        line=18,
        side="RIGHT",
        evidence=(
            "Only trusted constants call the helper in this snapshot, but a future "
            "untrusted caller would create an open redirect."
        ),
        suggested_fix="Accept a route identifier instead of an arbitrary target.",
    )
    report = ReviewReport(
        summary="One vulnerability and one preventative risk were found.",
        risk_score=7,
        confidence_score=2,
        diagram=ReviewDiagram(
            kind="sequence",
            title="Validated request flow",
            mermaid=(
                "sequenceDiagram\n"
                "  Client->>API: request\n"
                "  API->>Validator: validate"
            ),
        ),
        diagram_collapsible=True,
        diagram_default_open=False,
        summary_section_collapsible=True,
        summary_section_default_open=False,
        issues_table_section_collapsible=True,
        issues_table_section_default_open=False,
        confidence_score_section_collapsible=True,
        confidence_score_section_default_open=False,
        footer_included=False,
        update_description=True,
        summary_comment_enabled=False,
        fix_with_agent_enabled=False,
        findings=[finding, preventative_finding],
        diff_file_count=2,
        reviewed_file_count=2,
        context_chunk_count=3,
        prompt_tokens=120,
        completion_tokens=30,
    )

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=event.repo_full_name,
            default_branch="main",
        )
        related_repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name="store/native-review-shared",
            default_branch="main",
        )
        related_snapshot = begin_index_snapshot(
            connection,
            related_repository.full_name,
            "6" * 40,
            "integration-context-model",
            1536,
        )
        context_snapshots = (
            RepositoryContextSnapshot(
                repository_id=related_repository.id,
                repository_full_name=related_repository.full_name,
                snapshot_id=related_snapshot.snapshot_id,
                commit_sha="6" * 40,
                source="cluster",
                cluster_ids=(91,),
            ),
        )
        job = _claimed_job(
            connection,
            event,
            payload_sha256="b" * 64,
            worker_id="native-review-worker",
        )

        run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            verifier_model="openai/test-verifier-model",
            provenance={
                "schema_version": "diffuse-review-provenance-v1",
                "classification": "ai_assisted",
                "model_family": "anthropic",
                "confidence": 0.9,
            },
            model_routing_reason="opposing_anthropic_reviewer",
            # Routing permutes the configured pair per pull request, so what the
            # models were actually asked to do is not derivable from
            # configuration after the fact. It is recorded with the run, where
            # LOG_LEVEL cannot delete it.
            review_depth_resolution=(
                "REVIEW_DEPTH=exhaustive (routed by provenance: "
                "opposing_anthropic_reviewer) | candidate: openai/test-review-model "
                "has no reasoning control on this route"
            ),
            prompt_version="native-review-v1",
            context_fingerprint="c" * 64,
            context_snapshots=context_snapshots,
        )
        assert run.needs_generation
        persist_review_report(connection, run.id, report)
        assert load_review_report(connection, run.id) == report

        first_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert first_publication.status == "publishing"
        assert first_publication.review_number == 1
        mark_publication_failed(connection, first_publication.id)

        retry_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert retry_publication.id == first_publication.id
        assert retry_publication.review_number == 1
        mark_publication_published(
            connection,
            retry_publication.id,
            external_id="github-review-123",
            external_url=(
                "https://github.com/store/native-review/pull/23#pullrequestreview-123"
            ),
        )

        resumed_run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            prompt_version="native-review-v1",
            context_fingerprint="c" * 64,
        )
        resumed_publication = begin_publication(
            connection,
            run.id,
            scm_provider="github",
        )
        assert resumed_run.status == "published"
        assert not resumed_run.needs_generation
        assert resumed_publication.status == "published"
        assert resumed_publication.external_id == "github-review-123"
        assert resumed_publication.review_number == 1

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    run.status,
                    publication.status,
                    publication.attempt_count,
                    run.confidence_score,
                    run.review_number,
                    run.verifier_model,
                    run.provenance->>'model_family',
                    run.model_routing_reason,
                    run.review_depth_resolution
                FROM review_runs AS run
                JOIN review_publications AS publication
                  ON publication.review_run_id = run.id
                WHERE run.id = %s
                """,
                (run.id,),
            )
            statuses = cursor.fetchone()
            cursor.execute(
                """
                SELECT fingerprint, security_classification
                FROM review_findings
                WHERE review_run_id = %s
                ORDER BY ordinal
                """,
                (run.id,),
            )
            finding_classifications = cursor.fetchall()
            cursor.execute(
                """
                SELECT
                    repository_id,
                    repository_full_name,
                    snapshot_id,
                    commit_sha,
                    relation_kind,
                    cluster_ids,
                    ordinal
                FROM review_run_context_snapshots
                WHERE review_run_id = %s
                """,
                (run.id,),
            )
            context_state = cursor.fetchone()
        connection.rollback()

    assert statuses == (
        "published",
        "published",
        2,
        2,
        1,
        "openai/test-verifier-model",
        "anthropic",
        "opposing_anthropic_reviewer",
        "REVIEW_DEPTH=exhaustive (routed by provenance: "
        "opposing_anthropic_reviewer) | candidate: openai/test-review-model "
        "has no reasoning control on this route",
    )
    assert finding_classifications == [
        (finding.fingerprint, "vulnerability"),
        (preventative_finding.fingerprint, "preventative"),
    ]
    assert context_state == (
        related_repository.id,
        "store/native-review-shared",
        related_snapshot.snapshot_id,
        "6" * 40,
        "cluster",
        [91],
        1,
    )


def test_a_skipped_review_run_round_trips_and_never_publishes():
    """A policy skip is a durable outcome, not an absent one.

    `skip_reason` and the disabled publication flags have to survive the same
    round trip as a full report, and a skipped run must leave
    `review_publications` empty.
    """
    event = PullRequestEvent.from_payload(
        {
            "provider": "github",
            "scm_base_url": "https://github.com",
            "api_base_url": "https://api.github.com",
            "repo_full_name": "store/trigger-policy",
            "number": 31,
            "web_url": "https://github.com/store/trigger-policy/pull/31",
            "action": "ready_for_review",
            "head_sha": "4" * 40,
            "base_sha": "3" * 40,
            "updated_at": "2026-07-23T18:01:00Z",
            "delivery_id": "trigger-ready",
            "author": "octocat",
            "base_branch": "main",
            "head_branch": "feature/trigger-policy",
            "is_draft": False,
            "labels": ["needs-review"],
            "title": "Add trigger policy",
            "description": "Exercises metadata-sensitive review identity.",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 2,
        }
    )
    skipped_report = ReviewReport(
        summary="Automatic review is disabled by repository policy.",
        risk_score=0,
        findings=[],
        diff_file_count=2,
        reviewed_file_count=0,
        ignored_file_count=0,
        inline_comments_enabled=False,
        publication_enabled=False,
        skip_reason="automatic_disabled",
        context_chunk_count=0,
        prompt_tokens=0,
        completion_tokens=0,
    )

    with closing(psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])) as connection:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.com",
            full_name=event.repo_full_name,
            default_branch="main",
        )
        job = _claimed_job(
            connection,
            event,
            payload_sha256="e" * 64,
            worker_id="trigger-worker",
        )
        run = begin_review_run(
            connection,
            workflow_job_id=job.id,
            repository_id=repository.id,
            pull_request_id=job.pull_request_id,
            index_snapshot_id=None,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            model="openai/test-review-model",
            prompt_version="native-review-v2-repository-policy",
            context_fingerprint="d" * 64,
        )
        persist_review_report(connection, run.id, skipped_report)
        assert load_review_report(connection, run.id) == skipped_report

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, skip_reason FROM review_runs WHERE id = %s",
                (run.id,),
            )
            run_state = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM review_publications WHERE review_run_id = %s",
                (run.id,),
            )
            publication_count = cursor.fetchone()[0]
        connection.rollback()

    assert run_state == ("skipped", "automatic_disabled")
    assert publication_count == 0
