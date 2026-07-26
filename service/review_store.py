"""Persistence for native review runs, findings, and SCM publications."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import psycopg2.extras

from repository_policy.resolve import ApprovedCustomContext, ApprovedLearnedRule
from retriever.context_models import RepositoryContextSnapshot
from service.feedback_store import record_review_outcomes
from service.finding_store import (
    activate_finding_lineage_events,
    discard_unpublished_finding_lineage,
    persist_finding_lineage,
)
from service.review_models import ReviewDiagram, ReviewFinding, ReviewReport


@dataclass(frozen=True)
class ReviewRunHandle:
    id: int
    status: str
    index_snapshot_id: int | None

    @property
    def needs_generation(self) -> bool:
        return self.status == "generating"


@dataclass(frozen=True)
class PublicationHandle:
    id: int
    status: str
    idempotency_key: str
    external_id: str | None
    external_url: str | None
    review_number: int = 1


def _replace_review_run_learned_rules(
    cursor,
    review_run_id: int,
    learned_rules: tuple[ApprovedLearnedRule, ...],
) -> None:
    ids = [rule.id for rule in learned_rules]
    if len(ids) != len(set(ids)):
        raise ValueError("Review-run learned rules must have unique IDs")
    cursor.execute(
        "DELETE FROM review_run_learned_rules WHERE review_run_id = %s",
        (review_run_id,),
    )
    for rule in learned_rules:
        cursor.execute(
            """
            INSERT INTO review_run_learned_rules (
                review_run_id,
                learned_rule_id,
                rule_version,
                snapshot
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                review_run_id,
                rule.id,
                rule.version,
                psycopg2.extras.Json(
                    {
                        "id": rule.id,
                        "version": rule.version,
                        "title": rule.title,
                        "guidance": rule.guidance,
                        "applies_to": list(rule.applies_to),
                        "severity": rule.severity,
                        "category": rule.category,
                    }
                ),
            ),
        )


def _replace_review_run_custom_contexts(
    cursor,
    review_run_id: int,
    custom_contexts: tuple[ApprovedCustomContext, ...],
) -> None:
    ids = [context.id for context in custom_contexts]
    if len(ids) != len(set(ids)):
        raise ValueError("Review-run custom contexts must have unique IDs")
    cursor.execute(
        "DELETE FROM review_run_custom_contexts WHERE review_run_id = %s",
        (review_run_id,),
    )
    for context in custom_contexts:
        cursor.execute(
            """
            INSERT INTO review_run_custom_contexts (
                review_run_id,
                custom_context_id,
                snapshot
            )
            VALUES (%s, %s, %s)
            """,
            (
                review_run_id,
                context.id,
                psycopg2.extras.Json(
                    {
                        "id": context.id,
                        "context_type": context.context_type,
                        "body": context.body,
                        "applies_to": list(context.applies_to),
                        "metadata": context.metadata,
                    }
                ),
            ),
        )


def _replace_review_run_context_snapshots(
    cursor,
    review_run_id: int,
    context_snapshots: tuple[RepositoryContextSnapshot, ...],
) -> None:
    repository_ids = [item.repository_id for item in context_snapshots]
    if len(repository_ids) != len(set(repository_ids)) or len(context_snapshots) > 7:
        raise ValueError("Review-run context snapshots must be unique and bounded")
    cursor.execute(
        "DELETE FROM review_run_context_snapshots WHERE review_run_id = %s",
        (review_run_id,),
    )
    for ordinal, item in enumerate(context_snapshots, start=1):
        cursor.execute(
            """
            SELECT repository.full_name, snapshot.commit_sha
            FROM index_snapshots AS snapshot
            JOIN repositories AS repository ON repository.id = snapshot.repository_id
            WHERE snapshot.id = %s
              AND snapshot.repository_id = %s
            """,
            (item.snapshot_id, item.repository_id),
        )
        identity = cursor.fetchone()
        if (
            not identity
            or identity[0] != item.repository_full_name
            or identity[1] != item.commit_sha
        ):
            raise ValueError("Related context snapshot identity does not match the index")
        cursor.execute(
            """
            INSERT INTO review_run_context_snapshots (
                review_run_id,
                repository_id,
                repository_full_name,
                snapshot_id,
                commit_sha,
                relation_kind,
                cluster_ids,
                ordinal
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                review_run_id,
                item.repository_id,
                item.repository_full_name,
                item.snapshot_id,
                item.commit_sha,
                item.source,
                list(item.cluster_ids),
                ordinal,
            ),
        )


def begin_review_run(
    conn,
    *,
    workflow_job_id: int,
    repository_id: int,
    pull_request_id: int,
    index_snapshot_id: int | None,
    base_sha: str,
    head_sha: str,
    model: str,
    prompt_version: str,
    context_fingerprint: str,
    verifier_model: str | None = None,
    provenance: dict[str, object] | None = None,
    model_routing_reason: str = "legacy_single_model",
    learned_rules: tuple[ApprovedLearnedRule, ...] = (),
    custom_contexts: tuple[ApprovedCustomContext, ...] = (),
    context_snapshots: tuple[RepositoryContextSnapshot, ...] = (),
) -> ReviewRunHandle:
    if not re.fullmatch(r"[0-9a-f]{64}", context_fingerprint):
        raise ValueError("Review context fingerprint must be a lowercase SHA-256 value")
    selected_verifier_model = verifier_model or model
    selected_provenance = {} if provenance is None else provenance
    if not model.strip() or not selected_verifier_model.strip():
        raise ValueError("Review models cannot be empty")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", model_routing_reason):
        raise ValueError("Review model routing reason is invalid")
    if (
        not isinstance(selected_provenance, dict)
        or len(
            json.dumps(
                selected_provenance,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        > 65_536
    ):
        raise ValueError("Review provenance must be a bounded JSON object")
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT repository_id FROM pull_requests WHERE id = %s FOR UPDATE",
            (pull_request_id,),
        )
        pull_request = cursor.fetchone()
        if not pull_request:
            raise ValueError("Pull request does not exist")
        if int(pull_request[0]) != repository_id:
            raise ValueError("Pull request does not belong to the review repository")
        if index_snapshot_id is not None:
            cursor.execute(
                """
                SELECT 1
                FROM index_snapshots
                WHERE id = %s
                  AND repository_id = %s
                """,
                (index_snapshot_id, repository_id),
            )
            if not cursor.fetchone():
                raise ValueError("Index snapshot does not belong to the review repository")

        cursor.execute(
            """
            SELECT id, status, index_snapshot_id
            FROM review_runs
            WHERE workflow_job_id = %s
            FOR UPDATE
            """,
            (workflow_job_id,),
        )
        job_run = cursor.fetchone()
        if job_run:
            review_run_id = int(job_run[0])
            existing_status = job_run[1]
            if existing_status in {"failed", "generating"}:
                cursor.execute(
                    """
                    UPDATE review_runs
                    SET index_snapshot_id = %s,
                        model = %s,
                        verifier_model = %s,
                        provenance = %s,
                        model_routing_reason = %s,
                        prompt_version = %s,
                        context_fingerprint = %s,
                        status = 'generating',
                        failure_code = NULL,
                        started_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        index_snapshot_id,
                        model,
                        selected_verifier_model,
                        psycopg2.extras.Json(selected_provenance),
                        model_routing_reason,
                        prompt_version,
                        context_fingerprint,
                        review_run_id,
                    ),
                )
                _replace_review_run_learned_rules(
                    cursor,
                    review_run_id,
                    learned_rules,
                )
                _replace_review_run_custom_contexts(
                    cursor,
                    review_run_id,
                    custom_contexts,
                )
                _replace_review_run_context_snapshots(
                    cursor,
                    review_run_id,
                    context_snapshots,
                )
                return ReviewRunHandle(
                    id=review_run_id,
                    status="generating",
                    index_snapshot_id=index_snapshot_id,
                )
            return ReviewRunHandle(
                id=review_run_id,
                status=existing_status,
                index_snapshot_id=(
                    int(job_run[2]) if job_run[2] is not None else None
                ),
            )

        cursor.execute(
            """
            SELECT id, status, index_snapshot_id
            FROM review_runs
            WHERE pull_request_id = %s
              AND base_sha = %s
              AND head_sha = %s
              AND model = %s
              AND prompt_version = %s
              AND context_fingerprint = %s
            """,
            (
                pull_request_id,
                base_sha,
                head_sha,
                model,
                prompt_version,
                context_fingerprint,
            ),
        )
        existing = cursor.fetchone()
        if existing:
            review_run_id = int(existing[0])
            existing_status = existing[1]
            if existing_status in {"failed", "generating"}:
                cursor.execute(
                    """
                    UPDATE review_runs
                    SET workflow_job_id = %s,
                        index_snapshot_id = %s,
                        status = 'generating',
                        failure_code = NULL,
                        started_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (workflow_job_id, index_snapshot_id, review_run_id),
                )
                _replace_review_run_learned_rules(
                    cursor,
                    review_run_id,
                    learned_rules,
                )
                _replace_review_run_custom_contexts(
                    cursor,
                    review_run_id,
                    custom_contexts,
                )
                _replace_review_run_context_snapshots(
                    cursor,
                    review_run_id,
                    context_snapshots,
                )
                return ReviewRunHandle(
                    id=review_run_id,
                    status="generating",
                    index_snapshot_id=index_snapshot_id,
                )
            return ReviewRunHandle(
                id=review_run_id,
                status=existing_status,
                index_snapshot_id=(int(existing[2]) if existing[2] is not None else None),
            )

        cursor.execute(
            """
            INSERT INTO review_runs (
                workflow_job_id,
                repository_id,
                pull_request_id,
                index_snapshot_id,
                base_sha,
                head_sha,
                model,
                verifier_model,
                provenance,
                model_routing_reason,
                prompt_version,
                context_fingerprint,
                status
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'generating'
            )
            RETURNING id
            """,
            (
                workflow_job_id,
                repository_id,
                pull_request_id,
                index_snapshot_id,
                base_sha,
                head_sha,
                model,
                selected_verifier_model,
                psycopg2.extras.Json(selected_provenance),
                model_routing_reason,
                prompt_version,
                context_fingerprint,
            ),
        )
        review_run_id = int(cursor.fetchone()[0])
        _replace_review_run_learned_rules(
            cursor,
            review_run_id,
            learned_rules,
        )
        _replace_review_run_custom_contexts(
            cursor,
            review_run_id,
            custom_contexts,
        )
        _replace_review_run_context_snapshots(
            cursor,
            review_run_id,
            context_snapshots,
        )
        return ReviewRunHandle(
            id=review_run_id,
            status="generating",
            index_snapshot_id=index_snapshot_id,
        )


def persist_review_report(
    conn,
    review_run_id: int,
    report: ReviewReport,
    *,
    touched_paths: frozenset[str] = frozenset(),
    path_aliases: dict[str, str] | None = None,
) -> None:
    next_status = "ready" if report.publication_enabled else "skipped"
    aliases = path_aliases or {}
    if any(
        not isinstance(path, str) or not path or path.startswith("/")
        for path in touched_paths
    ):
        raise ValueError("Touched finding paths must be normalized repository paths")
    if any(
        not isinstance(old, str)
        or not isinstance(new, str)
        or not old
        or not new
        or old.startswith("/")
        or new.startswith("/")
        for old, new in aliases.items()
    ):
        raise ValueError("Finding path aliases must be normalized repository paths")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            UPDATE review_runs
            SET status = %s,
                summary = %s,
                risk_score = %s,
                confidence_score = %s,
                diagram_kind = %s,
                diagram_title = %s,
                diagram_mermaid = %s,
                diagram_collapsible = %s,
                diagram_default_open = %s,
                summary_section_included = %s,
                summary_section_collapsible = %s,
                summary_section_default_open = %s,
                issues_table_section_included = %s,
                issues_table_section_collapsible = %s,
                issues_table_section_default_open = %s,
                confidence_score_section_included = %s,
                confidence_score_section_collapsible = %s,
                confidence_score_section_default_open = %s,
                footer_included = %s,
                update_description = %s,
                summary_comment_enabled = %s,
                fix_with_agent_enabled = %s,
                context_chunk_count = %s,
                diff_file_count = %s,
                reviewed_file_count = %s,
                ignored_file_count = %s,
                inline_comments_enabled = %s,
                publication_enabled = %s,
                skip_reason = %s,
                prompt_tokens = %s,
                completion_tokens = %s,
                failure_code = NULL,
                ready_at = now(),
                updated_at = now()
            WHERE id = %s
              AND status = 'generating'
            RETURNING pull_request_id
            """,
            (
                next_status,
                report.summary,
                report.risk_score,
                report.confidence_score,
                report.diagram.kind.value if report.diagram is not None else None,
                report.diagram.title if report.diagram is not None else None,
                report.diagram.mermaid if report.diagram is not None else None,
                report.diagram_collapsible,
                report.diagram_default_open,
                report.summary_section_included,
                report.summary_section_collapsible,
                report.summary_section_default_open,
                report.issues_table_section_included,
                report.issues_table_section_collapsible,
                report.issues_table_section_default_open,
                report.confidence_score_section_included,
                report.confidence_score_section_collapsible,
                report.confidence_score_section_default_open,
                report.footer_included,
                report.update_description,
                report.summary_comment_enabled,
                report.fix_with_agent_enabled,
                report.context_chunk_count,
                report.diff_file_count,
                report.reviewed_file_count,
                report.ignored_file_count,
                report.inline_comments_enabled,
                report.publication_enabled,
                report.skip_reason,
                report.prompt_tokens,
                report.completion_tokens,
                review_run_id,
            ),
        )
        review_run = cursor.fetchone()
        if not review_run:
            raise RuntimeError("Review run is not in a generatable state")
        # Lineage events reference findings with ON DELETE SET NULL, but
        # non-addressed transitions require finding_id. Clear unpublished
        # projections before replacing findings on regenerate.
        cursor.execute(
            """
            DELETE FROM finding_lineage_events
            WHERE review_run_id = %s
              AND applied_at IS NULL
            """,
            (review_run_id,),
        )
        cursor.execute(
            "DELETE FROM review_findings WHERE review_run_id = %s",
            (review_run_id,),
        )
        cursor.execute(
            """
            DELETE FROM finding_lineages
            WHERE first_seen_review_run_id = %s
              AND status = 'pending'
            """,
            (review_run_id,),
        )
        persist_finding_lineage(
            cursor,
            pull_request_id=int(review_run["pull_request_id"]),
            review_run_id=review_run_id,
            findings=tuple(report.findings),
            touched_paths=touched_paths if report.publication_enabled else frozenset(),
            path_aliases=aliases if report.publication_enabled else None,
        )


def load_review_report(conn, review_run_id: int) -> ReviewReport:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                summary,
                risk_score,
                confidence_score,
                diagram_kind,
                diagram_title,
                diagram_mermaid,
                diagram_collapsible,
                diagram_default_open,
                summary_section_included,
                summary_section_collapsible,
                summary_section_default_open,
                issues_table_section_included,
                issues_table_section_collapsible,
                issues_table_section_default_open,
                confidence_score_section_included,
                confidence_score_section_collapsible,
                confidence_score_section_default_open,
                footer_included,
                update_description,
                summary_comment_enabled,
                fix_with_agent_enabled,
                diff_file_count,
                reviewed_file_count,
                ignored_file_count,
                inline_comments_enabled,
                publication_enabled,
                skip_reason,
                context_chunk_count,
                prompt_tokens,
                completion_tokens
            FROM review_runs
            WHERE id = %s
              AND status IN ('ready', 'skipped', 'publishing', 'published')
            """,
            (review_run_id,),
        )
        run = cursor.fetchone()
        if not run:
            raise RuntimeError("Review report is not ready")
        cursor.execute(
            """
            SELECT
                fingerprint,
                title,
                body,
                severity,
                category,
                security_classification,
                confidence,
                file_path,
                line,
                side,
                evidence,
                suggested_fix
            FROM review_findings
            WHERE review_run_id = %s
            ORDER BY ordinal
            """,
            (review_run_id,),
        )
        findings = [ReviewFinding.model_validate(dict(row)) for row in cursor.fetchall()]
    return ReviewReport(
        summary=run["summary"],
        risk_score=float(run["risk_score"]),
        confidence_score=int(run["confidence_score"]),
        diagram=(
            ReviewDiagram(
                kind=run["diagram_kind"],
                title=run["diagram_title"],
                mermaid=run["diagram_mermaid"],
            )
            if run["diagram_kind"] is not None
            else None
        ),
        diagram_collapsible=bool(run["diagram_collapsible"]),
        diagram_default_open=bool(run["diagram_default_open"]),
        summary_section_included=bool(run["summary_section_included"]),
        summary_section_collapsible=bool(run["summary_section_collapsible"]),
        summary_section_default_open=bool(run["summary_section_default_open"]),
        issues_table_section_included=bool(
            run["issues_table_section_included"]
        ),
        issues_table_section_collapsible=bool(
            run["issues_table_section_collapsible"]
        ),
        issues_table_section_default_open=bool(
            run["issues_table_section_default_open"]
        ),
        confidence_score_section_included=bool(
            run["confidence_score_section_included"]
        ),
        confidence_score_section_collapsible=bool(
            run["confidence_score_section_collapsible"]
        ),
        confidence_score_section_default_open=bool(
            run["confidence_score_section_default_open"]
        ),
        footer_included=bool(run["footer_included"]),
        update_description=bool(run["update_description"]),
        summary_comment_enabled=bool(run["summary_comment_enabled"]),
        fix_with_agent_enabled=bool(run["fix_with_agent_enabled"]),
        findings=findings,
        diff_file_count=int(run["diff_file_count"]),
        reviewed_file_count=int(run["reviewed_file_count"]),
        ignored_file_count=int(run["ignored_file_count"]),
        inline_comments_enabled=bool(run["inline_comments_enabled"]),
        publication_enabled=bool(run["publication_enabled"]),
        skip_reason=run["skip_reason"],
        context_chunk_count=int(run["context_chunk_count"]),
        prompt_tokens=int(run["prompt_tokens"]),
        completion_tokens=int(run["completion_tokens"]),
    )


def mark_review_failed(
    conn,
    review_run_id: int,
    error_code: str = "review_generation_failed",
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'failed',
                failure_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status = 'generating'
            """,
            (error_code, review_run_id),
        )


def mark_review_superseded(conn, review_run_id: int) -> None:
    discard_unpublished_finding_lineage(conn, review_run_id)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'superseded',
                updated_at = now()
            WHERE id = %s
              AND status IN ('generating', 'ready', 'publishing')
            """,
            (review_run_id,),
        )


def mark_review_terminal_failed(
    conn,
    workflow_job_id: int,
    *,
    error_code: str = "review_workflow_exhausted",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Review failure code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status
            FROM review_runs
            WHERE workflow_job_id = %s
            FOR UPDATE
            """,
            (workflow_job_id,),
        )
        row = cursor.fetchone()
        if not row or row[1] in {"published", "skipped", "superseded"}:
            return
        review_run_id = int(row[0])
    discard_unpublished_finding_lineage(conn, review_run_id)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'failed',
                failure_code = %s,
                updated_at = now()
            WHERE id = %s
              AND status IN ('generating', 'ready', 'publishing')
            """,
            (error_code, review_run_id),
        )


def begin_publication(
    conn,
    review_run_id: int,
    *,
    scm_provider: str,
) -> PublicationHandle:
    idempotency_key = f"review-run:{review_run_id}:pull-request-review"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT pull_request_id, status, publication_enabled, review_number
            FROM review_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (review_run_id,),
        )
        review_run = cursor.fetchone()
        if (
            not review_run
            or not review_run["publication_enabled"]
            or review_run["status"] not in {"ready", "publishing", "published"}
        ):
            raise RuntimeError("Review run is not publishable")
        cursor.execute(
            "SELECT id FROM pull_requests WHERE id = %s FOR UPDATE",
            (int(review_run["pull_request_id"]),),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("Review run pull request no longer exists")
        review_number = review_run["review_number"]
        if review_number is None:
            cursor.execute(
                """
                SELECT
                    COALESCE(MAX(review_number), 0) + 1
                    AS next_review_number
                FROM review_runs
                WHERE pull_request_id = %s
                """,
                (int(review_run["pull_request_id"]),),
            )
            review_number = int(cursor.fetchone()["next_review_number"])
            cursor.execute(
                """
                UPDATE review_runs
                SET review_number = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (review_number, review_run_id),
            )
        cursor.execute(
            """
            INSERT INTO review_publications (
                review_run_id,
                scm_provider,
                publication_kind,
                idempotency_key,
                status
            )
            VALUES (%s, %s, 'pull_request_review', %s, 'pending')
            ON CONFLICT (review_run_id, scm_provider, publication_kind)
            DO NOTHING
            """,
            (review_run_id, scm_provider, idempotency_key),
        )
        cursor.execute(
            """
            SELECT id, status, idempotency_key, external_id, external_url
            FROM review_publications
            WHERE review_run_id = %s
              AND scm_provider = %s
              AND publication_kind = 'pull_request_review'
            FOR UPDATE
            """,
            (review_run_id, scm_provider),
        )
        publication = cursor.fetchone()
        if publication["status"] == "published":
            return PublicationHandle(
                id=int(publication["id"]),
                status="published",
                idempotency_key=publication["idempotency_key"],
                external_id=publication["external_id"],
                external_url=publication["external_url"],
                review_number=int(review_number),
            )
        cursor.execute(
            """
            UPDATE review_publications
            SET status = 'publishing',
                attempt_count = attempt_count + 1,
                error_code = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (int(publication["id"]),),
        )
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'publishing',
                updated_at = now()
            WHERE id = %s
              AND status IN ('ready', 'publishing')
            """,
            (review_run_id,),
        )
        return PublicationHandle(
            id=int(publication["id"]),
            status="publishing",
            idempotency_key=publication["idempotency_key"],
            external_id=publication["external_id"],
            external_url=publication["external_url"],
            review_number=int(review_number),
        )


def mark_publication_published(
    conn,
    publication_id: int,
    *,
    external_id: str,
    external_url: str | None,
    unanchored_fingerprints: frozenset[str] = frozenset(),
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_publications
            SET status = 'published',
                external_id = %s,
                external_url = %s,
                error_code = NULL,
                published_at = now(),
                updated_at = now()
            WHERE id = %s
            RETURNING review_run_id
            """,
            (external_id, external_url, publication_id),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Review publication does not exist")
        activate_finding_lineage_events(
            conn,
            int(row[0]),
            unanchored_fingerprints=unanchored_fingerprints,
        )
        record_review_outcomes(conn, int(row[0]))
        cursor.execute(
            """
            UPDATE review_runs
            SET status = 'published',
                published_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (int(row[0]),),
        )


def mark_publication_failed(
    conn,
    publication_id: int,
    *,
    error_code: str = "review_publication_failed",
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE review_publications
            SET status = 'failed',
                error_code = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING review_run_id
            """,
            (error_code, publication_id),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                """
                UPDATE review_runs
                SET status = 'ready',
                    failure_code = %s,
                    updated_at = now()
                WHERE id = %s
                  AND status = 'publishing'
                """,
                (error_code, int(row[0])),
            )
