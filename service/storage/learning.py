"""Durable generation, moderation, and loading of feedback-derived rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import psycopg2.extras

from repository_policy.models import RepositoryRule
from repository_policy.resolve import ApprovedLearnedRule
from service.models.learning import (
    LearnedRuleRecord,
    RuleLearningEvidence,
    RuleLearningJobEvent,
    RuleLearningResult,
    RuleLearningWork,
    SuggestedRuleBatch,
    SuggestedRuleCandidate,
    candidate_deduplication_key,
    candidate_similarity_text,
    evidence_fingerprint,
)

MAX_LEARNING_EVIDENCE = 500
MODERATOR_AUTHORITIES = {"OWNER", "ADMIN", "MEMBER", "COLLABORATOR", "OPERATOR"}
MODERATION_ACTIONS = {"edit", "approve", "reject", "deactivate", "reactivate"}


@dataclass(frozen=True)
class RuleLearningEnqueueResult:
    job_id: int | None
    state: str

    @property
    def accepted(self) -> bool:
        return self.state == "queued"


def load_rule_learning_evidence(
    conn,
    *,
    repository_id: int,
    limit: int = MAX_LEARNING_EVIDENCE,
) -> tuple[RuleLearningEvidence, ...]:
    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    if not 1 <= limit <= MAX_LEARNING_EVIDENCE:
        raise ValueError(f"Rule-learning evidence limit must be 1 to {MAX_LEARNING_EVIDENCE}")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            WITH current_reactions AS (
                SELECT DISTINCT ON (finding_thread_id, source_external_id)
                    *
                FROM review_feedback_events
                WHERE repository_id = %s
                  AND source_kind = 'reaction'
                ORDER BY finding_thread_id, source_external_id, id DESC
            ),
            effective_events AS (
                SELECT *
                FROM review_feedback_events
                WHERE repository_id = %s
                  AND source_kind <> 'reaction'
                  AND event_action = 'observed'
                UNION ALL
                SELECT *
                FROM current_reactions
                WHERE event_action = 'observed'
            )
            SELECT
                event.id,
                event.pull_request_id,
                pull_request.number AS pull_request_number,
                event.source_kind,
                event.signal_kind,
                event.content,
                COALESCE(source_finding.title, current_finding.title) AS finding_title,
                COALESCE(source_finding.body, current_finding.body) AS finding_body,
                COALESCE(source_finding.file_path, current_finding.file_path) AS file_path,
                event.finding_category,
                event.finding_severity,
                event.finding_security_classification,
                event.suppression_protected
            FROM effective_events AS event
            JOIN pull_requests AS pull_request ON pull_request.id = event.pull_request_id
            JOIN finding_threads AS thread ON thread.id = event.finding_thread_id
            JOIN finding_lineages AS lineage ON lineage.id = thread.lineage_id
            LEFT JOIN review_findings AS source_finding ON source_finding.id = event.finding_id
            JOIN LATERAL (
                SELECT candidate.title, candidate.body, candidate.file_path
                FROM review_findings AS candidate
                JOIN finding_lineage_events AS occurrence
                  ON occurrence.finding_id = candidate.id
                 AND occurrence.applied_at IS NOT NULL
                WHERE candidate.lineage_id = lineage.id
                ORDER BY candidate.id DESC
                LIMIT 1
            ) AS current_finding ON TRUE
            ORDER BY event.id DESC
            LIMIT %s
            """,
            (repository_id, repository_id, limit),
        )
        rows = list(reversed(cursor.fetchall()))
    return tuple(
        RuleLearningEvidence(
            event_id=int(row["id"]),
            pull_request_id=int(row["pull_request_id"]),
            pull_request_number=int(row["pull_request_number"]),
            source_kind=row["source_kind"],
            signal_kind=row["signal_kind"],
            content=row["content"],
            finding_title=row["finding_title"],
            finding_body=row["finding_body"],
            file_path=row["file_path"],
            category=row["finding_category"],
            severity=row["finding_severity"],
            suppression_protected=bool(row["suppression_protected"]),
            security_classification=row["finding_security_classification"],
        )
        for row in rows
    )


def _eligible(
    evidence: tuple[RuleLearningEvidence, ...],
    *,
    minimum_evidence: int,
    minimum_pull_requests: int,
) -> bool:
    return len(evidence) >= minimum_evidence and len(
        {item.pull_request_id for item in evidence}
    ) >= minimum_pull_requests


def queue_rule_learning_job(
    conn,
    *,
    repository_id: int,
    minimum_evidence: int,
    minimum_pull_requests: int,
    evaluation_interval_seconds: int,
) -> RuleLearningEnqueueResult:
    if minimum_evidence <= 0 or minimum_pull_requests <= 0:
        raise ValueError("Rule-learning thresholds must be positive")
    if not 60 <= evaluation_interval_seconds <= 604_800:
        raise ValueError("Rule-learning evaluation interval must be 60 to 604800 seconds")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO suggested_rule_learning_states (repository_id)
            SELECT id
            FROM repositories
            WHERE id = %s
            ON CONFLICT (repository_id) DO NOTHING
            """,
            (repository_id,),
        )
        cursor.execute(
            """
            SELECT
                repository.id,
                repository.full_name,
                repository.enabled,
                state.generation,
                state.last_evidence_fingerprint
            FROM repositories AS repository
            JOIN suggested_rule_learning_states AS state
              ON state.repository_id = repository.id
            WHERE repository.id = %s
            FOR UPDATE OF state
            """,
            (repository_id,),
        )
        repository = cursor.fetchone()
        if not repository:
            raise ValueError("Repository does not exist")
        if not repository["enabled"]:
            return RuleLearningEnqueueResult(None, "repository_disabled")

        evidence = load_rule_learning_evidence(conn, repository_id=repository_id)
        if not _eligible(
            evidence,
            minimum_evidence=minimum_evidence,
            minimum_pull_requests=minimum_pull_requests,
        ):
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET next_evaluation_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (evaluation_interval_seconds, repository_id),
            )
            return RuleLearningEnqueueResult(None, "insufficient_evidence")

        fingerprint = evidence_fingerprint(evidence)
        if repository["last_evidence_fingerprint"] == fingerprint:
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET next_evaluation_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (evaluation_interval_seconds, repository_id),
            )
            return RuleLearningEnqueueResult(None, "unchanged_evidence")

        event = RuleLearningJobEvent(
            repository_id=repository_id,
            repo_full_name=repository["full_name"],
            generation=int(repository["generation"]) + 1,
            evidence_fingerprint=fingerprint,
        )
        cursor.execute(
            """
            SELECT id, status
            FROM workflow_jobs
            WHERE idempotency_key = %s
            """,
            (event.idempotency_key,),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET next_evaluation_at = now() + (%s * interval '1 second'),
                    last_scheduled_job_id = %s,
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (evaluation_interval_seconds, int(existing["id"]), repository_id),
            )
            return RuleLearningEnqueueResult(
                int(existing["id"]),
                f"duplicate:{existing['status']}",
            )

        cursor.execute(
            """
            SELECT 1
            FROM workflow_jobs
            WHERE scope_key = %s
              AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (event.scope_key,),
        )
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET next_evaluation_at = now() + interval '60 seconds',
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (repository_id,),
            )
            return RuleLearningEnqueueResult(None, "already_scheduled")

        cursor.execute(
            """
            INSERT INTO workflow_jobs (
                repository_id,
                pull_request_id,
                job_type,
                idempotency_key,
                scope_key,
                base_revision,
                revision,
                payload,
                priority
            )
            VALUES (%s, NULL, 'generate_suggested_rules', %s, %s, %s, %s, %s, -20)
            RETURNING id
            """,
            (
                repository_id,
                event.idempotency_key,
                event.scope_key,
                fingerprint,
                fingerprint,
                psycopg2.extras.Json(event.to_payload()),
            ),
        )
        job_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            UPDATE suggested_rule_learning_states
            SET generation = %s,
                next_evaluation_at = now() + (%s * interval '1 second'),
                last_scheduled_job_id = %s,
                updated_at = now()
            WHERE repository_id = %s
            """,
            (
                event.generation,
                evaluation_interval_seconds,
                job_id,
                repository_id,
            ),
        )
    return RuleLearningEnqueueResult(job_id, "queued")


def schedule_due_rule_learning_jobs(
    conn,
    *,
    minimum_evidence: int = 10,
    minimum_pull_requests: int = 10,
    evaluation_interval_seconds: int = 3600,
    limit: int = 5,
) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("Rule-learning scheduler limit must be 1 to 100")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO suggested_rule_learning_states (repository_id)
            SELECT id
            FROM repositories
            ON CONFLICT (repository_id) DO NOTHING
            """
        )
        cursor.execute(
            """
            SELECT state.repository_id
            FROM suggested_rule_learning_states AS state
            JOIN repositories AS repository ON repository.id = state.repository_id
            WHERE state.next_evaluation_at <= now()
              AND repository.enabled = TRUE
            ORDER BY state.next_evaluation_at, state.repository_id
            FOR UPDATE OF state SKIP LOCKED
            LIMIT %s
            """,
            (limit,),
        )
        repository_ids = [int(row["repository_id"]) for row in cursor.fetchall()]
    scheduled = 0
    for repository_id in repository_ids:
        result = queue_rule_learning_job(
            conn,
            repository_id=repository_id,
            minimum_evidence=minimum_evidence,
            minimum_pull_requests=minimum_pull_requests,
            evaluation_interval_seconds=evaluation_interval_seconds,
        )
        scheduled += int(result.accepted)
    return scheduled


def begin_rule_learning(
    conn,
    *,
    workflow_job_id: int,
    event: RuleLearningJobEvent,
    model: str,
    prompt_version: str,
) -> RuleLearningWork:
    evidence = load_rule_learning_evidence(conn, repository_id=event.repository_id)
    current_fingerprint = evidence_fingerprint(evidence)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                job.repository_id,
                job.pull_request_id,
                job.job_type,
                job.scope_key,
                job.revision,
                repository.full_name
            FROM workflow_jobs AS job
            JOIN repositories AS repository ON repository.id = job.repository_id
            WHERE job.id = %s
            FOR UPDATE OF job
            """,
            (workflow_job_id,),
        )
        job = cursor.fetchone()
        if (
            not job
            or int(job["repository_id"]) != event.repository_id
            or job["pull_request_id"] is not None
            or job["job_type"] != "generate_suggested_rules"
            or job["scope_key"] != event.scope_key
            or job["revision"] != event.evidence_fingerprint
            or job["full_name"] != event.repo_full_name
        ):
            raise ValueError("Rule-learning workflow identity does not match its job")

        cursor.execute(
            """
            SELECT id, status
            FROM suggested_rule_generation_runs
            WHERE workflow_job_id = %s
            FOR UPDATE
            """,
            (workflow_job_id,),
        )
        existing = cursor.fetchone()
        if current_fingerprint != event.evidence_fingerprint:
            if existing:
                run_id = int(existing["id"])
                cursor.execute(
                    """
                    UPDATE suggested_rule_generation_runs
                    SET status = 'stale',
                        evidence_count = %s,
                        completed_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (len(evidence), run_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO suggested_rule_generation_runs (
                        workflow_job_id,
                        repository_id,
                        evidence_fingerprint,
                        model,
                        prompt_version,
                        status,
                        evidence_count,
                        completed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'stale', %s, now())
                    RETURNING id
                    """,
                    (
                        workflow_job_id,
                        event.repository_id,
                        event.evidence_fingerprint,
                        model,
                        prompt_version,
                        len(evidence),
                    ),
                )
                run_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET next_evaluation_at = now(),
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (event.repository_id,),
            )
            return RuleLearningWork(
                run_id=run_id,
                status="stale",
                evidence=evidence,
                evidence_fingerprint=current_fingerprint,
            )

        if existing and existing["status"] == "ready":
            return RuleLearningWork(
                run_id=int(existing["id"]),
                status="ready",
                evidence=evidence,
                evidence_fingerprint=current_fingerprint,
            )
        if existing:
            run_id = int(existing["id"])
            cursor.execute(
                """
                UPDATE suggested_rule_generation_runs
                SET status = 'generating',
                    model = %s,
                    prompt_version = %s,
                    evidence_count = %s,
                    failure_code = NULL,
                    started_at = now(),
                    completed_at = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (model, prompt_version, len(evidence), run_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO suggested_rule_generation_runs (
                    workflow_job_id,
                    repository_id,
                    evidence_fingerprint,
                    model,
                    prompt_version,
                    status,
                    evidence_count
                )
                VALUES (%s, %s, %s, %s, %s, 'generating', %s)
                RETURNING id
                """,
                (
                    workflow_job_id,
                    event.repository_id,
                    event.evidence_fingerprint,
                    model,
                    prompt_version,
                    len(evidence),
                ),
            )
            run_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            UPDATE suggested_rule_learning_states
            SET last_started_at = now(),
                last_error_code = NULL,
                updated_at = now()
            WHERE repository_id = %s
            """,
            (event.repository_id,),
        )
    return RuleLearningWork(
        run_id=run_id,
        status="generating",
        evidence=evidence,
        evidence_fingerprint=current_fingerprint,
    )


def _rule_snapshot(row: dict[str, object]) -> dict[str, object]:
    return {
        "status": row["status"],
        "version": int(row["version"]),
        "title": row["title"],
        "guidance": row["guidance"],
        "applies_to": list(row["applies_to"]),
        "severity": row["severity"],
        "category": row["category"],
        "evidence_count": int(row["evidence_count"]),
    }


def _matching_rule_id(
    rules: list[dict[str, object]],
    candidate: SuggestedRuleCandidate,
) -> int | None:
    key = candidate_deduplication_key(candidate)
    exact = next(
        (int(row["id"]) for row in rules if row["deduplication_key"] == key),
        None,
    )
    if exact is not None:
        return exact
    candidate_text = candidate_similarity_text(candidate)
    for row in rules:
        if (
            row["category"] != candidate.category
            or tuple(row["applies_to"]) != candidate.applies_to
        ):
            continue
        existing = SuggestedRuleCandidate(
            title=str(row["title"]),
            guidance=str(row["guidance"]),
            applies_to=tuple(row["applies_to"]),
            severity=str(row["severity"]),
            category=str(row["category"]),
            evidence_event_ids=(1,),
        )
        if SequenceMatcher(
            None,
            candidate_text,
            candidate_similarity_text(existing),
        ).ratio() >= 0.92:
            return int(row["id"])
    return None


def persist_rule_suggestions(
    conn,
    *,
    work: RuleLearningWork,
    suggestions: SuggestedRuleBatch,
    minimum_support: int,
    minimum_support_pull_requests: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> RuleLearningResult:
    if work.run_id is None or not work.needs_generation:
        raise ValueError("Rule-learning work is not generatable")
    if min(minimum_support, minimum_support_pull_requests) <= 0:
        raise ValueError("Suggested-rule support thresholds must be positive")
    if min(prompt_tokens, completion_tokens) < 0:
        raise ValueError("Suggested-rule token counts cannot be negative")

    evidence_by_id = {item.event_id: item for item in work.evidence}
    proposed = 0
    consolidated = 0
    rejected = 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT repository_id, evidence_fingerprint, status
            FROM suggested_rule_generation_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (work.run_id,),
        )
        run = cursor.fetchone()
        if (
            not run
            or run["status"] != "generating"
            or run["evidence_fingerprint"] != work.evidence_fingerprint
        ):
            raise RuntimeError("Suggested-rule generation run is not writable")
        repository_id = int(run["repository_id"])
        cursor.execute(
            """
            SELECT
                id,
                deduplication_key,
                status,
                version,
                title,
                guidance,
                applies_to,
                severity,
                category,
                evidence_count
            FROM learned_rules
            WHERE repository_id = %s
            ORDER BY id
            FOR UPDATE
            """,
            (repository_id,),
        )
        rules = [dict(row) for row in cursor.fetchall()]

        for candidate in suggestions.suggestions:
            cited = [
                evidence_by_id[event_id]
                for event_id in candidate.evidence_event_ids
                if event_id in evidence_by_id
            ]
            if (
                len(cited) != len(candidate.evidence_event_ids)
                or len(cited) < minimum_support
                or len({item.pull_request_id for item in cited})
                < minimum_support_pull_requests
            ):
                rejected += 1
                continue

            learned_rule_id = _matching_rule_id(rules, candidate)
            is_new = learned_rule_id is None
            if is_new:
                cursor.execute(
                    """
                    INSERT INTO learned_rules (
                        repository_id,
                        generated_run_id,
                        deduplication_key,
                        status,
                        title,
                        guidance,
                        applies_to,
                        severity,
                        category
                    )
                    VALUES (%s, %s, %s, 'suggested', %s, %s, %s, %s, %s)
                    RETURNING
                        id,
                        deduplication_key,
                        status,
                        version,
                        title,
                        guidance,
                        applies_to,
                        severity,
                        category,
                        evidence_count
                    """,
                    (
                        repository_id,
                        work.run_id,
                        candidate_deduplication_key(candidate),
                        candidate.title,
                        candidate.guidance,
                        list(candidate.applies_to),
                        candidate.severity,
                        candidate.category,
                    ),
                )
                rule = dict(cursor.fetchone())
                rules.append(rule)
                learned_rule_id = int(rule["id"])
                proposed += 1
            else:
                rule = next(row for row in rules if int(row["id"]) == learned_rule_id)
                consolidated += 1

            added = 0
            for evidence in cited:
                cursor.execute(
                    """
                    INSERT INTO suggested_rule_evidence (
                        learned_rule_id,
                        feedback_event_id,
                        generation_run_id
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT (learned_rule_id, feedback_event_id) DO NOTHING
                    """,
                    (learned_rule_id, evidence.event_id, work.run_id),
                )
                added += cursor.rowcount
            cursor.execute(
                """
                UPDATE learned_rules
                SET evidence_count = (
                        SELECT count(*)
                        FROM suggested_rule_evidence
                        WHERE learned_rule_id = %s
                    ),
                    updated_at = now()
                WHERE id = %s
                RETURNING
                    id,
                    deduplication_key,
                    status,
                    version,
                    title,
                    guidance,
                    applies_to,
                    severity,
                    category,
                    evidence_count
                """,
                (learned_rule_id, learned_rule_id),
            )
            updated_rule = dict(cursor.fetchone())
            rules[rules.index(rule)] = updated_rule
            if is_new or added:
                action = "proposed" if is_new else "evidence_added"
                cursor.execute(
                    """
                    INSERT INTO learned_rule_events (
                        learned_rule_id,
                        generation_run_id,
                        action,
                        event_key,
                        rule_version,
                        snapshot
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (learned_rule_id, event_key) DO NOTHING
                    """,
                    (
                        learned_rule_id,
                        work.run_id,
                        action,
                        f"generation-run:{work.run_id}:{action}",
                        int(updated_rule["version"]),
                        psycopg2.extras.Json(_rule_snapshot(updated_rule)),
                    ),
                )

        cursor.execute(
            """
            UPDATE suggested_rule_generation_runs
            SET status = 'ready',
                proposed_count = %s,
                consolidated_count = %s,
                rejected_count = %s,
                prompt_tokens = %s,
                completion_tokens = %s,
                failure_code = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (
                proposed,
                consolidated,
                rejected,
                prompt_tokens,
                completion_tokens,
                work.run_id,
            ),
        )
        cursor.execute(
            """
            UPDATE suggested_rule_learning_states
            SET last_evidence_fingerprint = %s,
                last_completed_at = now(),
                last_error_code = NULL,
                updated_at = now()
            WHERE repository_id = %s
            """,
            (work.evidence_fingerprint, repository_id),
        )
    return RuleLearningResult(proposed, consolidated, rejected)


def mark_rule_learning_failed(
    conn,
    *,
    workflow_job_id: int,
    error_code: str = "suggested_rule_generation_failed",
) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("Rule-learning error code is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE suggested_rule_generation_runs
            SET status = 'failed',
                failure_code = %s,
                updated_at = now()
            WHERE workflow_job_id = %s
              AND status = 'generating'
            RETURNING repository_id
            """,
            (error_code, workflow_job_id),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                """
                UPDATE suggested_rule_learning_states
                SET last_error_code = %s,
                    updated_at = now()
                WHERE repository_id = %s
                """,
                (error_code, int(row[0])),
            )


def _validated_rule(
    *,
    learned_rule_id: int,
    title: str,
    guidance: str,
    applies_to: tuple[str, ...],
    severity: str,
    category: str,
) -> RepositoryRule:
    return RepositoryRule(
        id=f"learned-{learned_rule_id}",
        title=title,
        guidance=guidance,
        applies_to=applies_to,
        severity=severity,
        category=category,
    )


def moderate_learned_rule(
    conn,
    *,
    repository_id: int,
    learned_rule_id: int,
    action: str,
    actor_login: str,
    actor_authority: str,
    event_key: str,
    expected_version: int,
    title: str | None = None,
    guidance: str | None = None,
    applies_to: tuple[str, ...] | None = None,
    severity: str | None = None,
    category: str | None = None,
    reason: str | None = None,
) -> LearnedRuleRecord:
    if action not in MODERATION_ACTIONS:
        raise ValueError("Unsupported learned-rule moderation action")
    actor_login = actor_login.strip()
    if not actor_login or len(actor_login) > 255 or "\x00" in actor_login:
        raise ValueError("Learned-rule moderator identity is invalid")
    if actor_authority not in MODERATOR_AUTHORITIES:
        raise ValueError("Learned-rule moderator is not authorized")
    if not event_key or len(event_key) > 255 or any(ord(char) < 32 for char in event_key):
        raise ValueError("Learned-rule moderation event key is invalid")
    if expected_version <= 0:
        raise ValueError("Learned-rule expected version must be positive")
    if reason is not None:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("Learned-rule moderation reason is invalid")
    if action in {"reject", "deactivate"} and reason is None:
        raise ValueError(f"Learned-rule {action} requires a reason")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                repository_id,
                status,
                version,
                title,
                guidance,
                applies_to,
                severity,
                category,
                evidence_count
            FROM learned_rules
            WHERE id = %s
              AND repository_id = %s
            FOR UPDATE
            """,
            (learned_rule_id, repository_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("Learned rule does not exist in this repository")
        cursor.execute(
            """
            SELECT 1
            FROM learned_rule_events
            WHERE learned_rule_id = %s
              AND event_key = %s
            """,
            (learned_rule_id, event_key),
        )
        if cursor.fetchone():
            return _learned_rule_record(row)
        if int(row["version"]) != expected_version:
            raise RuntimeError("Learned rule changed; reload it before moderating")

        status = row["status"]
        next_status = status
        next_version = int(row["version"])
        next_title = row["title"]
        next_guidance = row["guidance"]
        next_applies_to = tuple(row["applies_to"])
        next_severity = row["severity"]
        next_category = row["category"]
        event_action = action

        if action == "edit":
            if status not in {"suggested", "inactive"}:
                raise ValueError("Only suggested or inactive rules may be edited")
            next_title = title if title is not None else next_title
            next_guidance = guidance if guidance is not None else next_guidance
            next_applies_to = applies_to if applies_to is not None else next_applies_to
            next_severity = severity if severity is not None else next_severity
            next_category = category if category is not None else next_category
            if (
                next_title,
                next_guidance,
                next_applies_to,
                next_severity,
                next_category,
            ) == (
                row["title"],
                row["guidance"],
                tuple(row["applies_to"]),
                row["severity"],
                row["category"],
            ):
                raise ValueError("Learned-rule edit must change at least one field")
            next_version += 1
            event_action = "edited"
        elif action == "approve":
            if status != "suggested":
                raise ValueError("Only suggested rules may be approved")
            next_status = "active"
            event_action = "approved"
        elif action == "reject":
            if status != "suggested":
                raise ValueError("Only suggested rules may be rejected")
            next_status = "rejected"
            event_action = "rejected"
        elif action == "deactivate":
            if status != "active":
                raise ValueError("Only active rules may be deactivated")
            next_status = "inactive"
            event_action = "deactivated"
        elif action == "reactivate":
            if status != "inactive":
                raise ValueError("Only inactive rules may be reactivated")
            next_status = "active"
            event_action = "reactivated"

        validated = _validated_rule(
            learned_rule_id=learned_rule_id,
            title=next_title,
            guidance=next_guidance,
            applies_to=next_applies_to,
            severity=next_severity,
            category=next_category,
        )
        cursor.execute(
            """
            UPDATE learned_rules
            SET status = %s,
                version = %s,
                title = %s,
                guidance = %s,
                applies_to = %s,
                severity = %s,
                category = %s,
                activated_at = CASE
                    WHEN %s = 'active' THEN COALESCE(activated_at, now())
                    ELSE activated_at
                END,
                deactivated_at = CASE
                    WHEN %s = 'inactive' THEN now()
                    ELSE deactivated_at
                END,
                rejected_at = CASE
                    WHEN %s = 'rejected' THEN now()
                    ELSE rejected_at
                END,
                updated_at = now()
            WHERE id = %s
            RETURNING
                id,
                repository_id,
                status,
                version,
                title,
                guidance,
                applies_to,
                severity,
                category,
                evidence_count
            """,
            (
                next_status,
                next_version,
                validated.title,
                validated.guidance,
                list(validated.applies_to),
                validated.severity,
                validated.category,
                next_status,
                next_status,
                next_status,
                learned_rule_id,
            ),
        )
        updated = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO learned_rule_events (
                learned_rule_id,
                action,
                event_key,
                actor_login,
                actor_authority,
                reason,
                rule_version,
                snapshot
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                learned_rule_id,
                event_action,
                event_key,
                actor_login,
                actor_authority,
                reason,
                int(updated["version"]),
                psycopg2.extras.Json(_rule_snapshot(updated)),
            ),
        )
    return _learned_rule_record(updated)


def _learned_rule_record(row) -> LearnedRuleRecord:
    return LearnedRuleRecord(
        id=int(row["id"]),
        repository_id=int(row["repository_id"]),
        status=row["status"],
        version=int(row["version"]),
        title=row["title"],
        guidance=row["guidance"],
        applies_to=tuple(row["applies_to"]),
        severity=row["severity"],
        category=row["category"],
        evidence_count=int(row["evidence_count"]),
    )


def list_learned_rules(
    conn,
    *,
    repository_id: int,
    status: str | None = None,
) -> tuple[LearnedRuleRecord, ...]:
    if status is not None and status not in {"suggested", "active", "inactive", "rejected"}:
        raise ValueError("Learned-rule status filter is invalid")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                repository_id,
                status,
                version,
                title,
                guidance,
                applies_to,
                severity,
                category,
                evidence_count
            FROM learned_rules
            WHERE repository_id = %s
              AND (%s IS NULL OR status = %s)
            ORDER BY id
            """,
            (repository_id, status, status),
        )
        rows = cursor.fetchall()
    return tuple(_learned_rule_record(row) for row in rows)


def load_active_learned_rules(
    conn,
    *,
    repository_id: int,
) -> tuple[ApprovedLearnedRule, ...]:
    records = list_learned_rules(
        conn,
        repository_id=repository_id,
        status="active",
    )
    if len(records) > 100:
        raise RuntimeError("Repository has more than 100 active learned rules")
    output = []
    for record in records:
        validated = _validated_rule(
            learned_rule_id=record.id,
            title=record.title,
            guidance=record.guidance,
            applies_to=record.applies_to,
            severity=record.severity,
            category=record.category,
        )
        output.append(
            ApprovedLearnedRule(
                id=record.id,
                version=record.version,
                title=validated.title,
                guidance=validated.guidance,
                applies_to=validated.applies_to,
                severity=validated.severity,
                category=validated.category,
            )
        )
    return tuple(output)


def load_learned_rule_audit(
    conn,
    *,
    repository_id: int,
    learned_rule_id: int,
) -> dict[str, object]:
    records = list_learned_rules(conn, repository_id=repository_id)
    record = next((item for item in records if item.id == learned_rule_id), None)
    if record is None:
        raise ValueError("Learned rule does not exist in this repository")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                feedback.id AS event_id,
                pull_request.number AS pull_request_number,
                feedback.source_kind,
                feedback.signal_kind,
                feedback.event_action,
                feedback.content,
                feedback.finding_category,
                feedback.finding_severity,
                feedback.finding_security_classification,
                feedback.suppression_protected,
                evidence.created_at
            FROM suggested_rule_evidence AS evidence
            JOIN review_feedback_events AS feedback
              ON feedback.id = evidence.feedback_event_id
            JOIN pull_requests AS pull_request ON pull_request.id = feedback.pull_request_id
            WHERE evidence.learned_rule_id = %s
            ORDER BY feedback.id
            """,
            (learned_rule_id,),
        )
        evidence = [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key != "created_at"
                },
                "cited_at": row["created_at"].isoformat(),
            }
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT
                action,
                event_key,
                actor_login,
                actor_authority,
                reason,
                rule_version,
                snapshot,
                created_at
            FROM learned_rule_events
            WHERE learned_rule_id = %s
            ORDER BY id
            """,
            (learned_rule_id,),
        )
        history = [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key != "created_at"
                },
                "created_at": row["created_at"].isoformat(),
            }
            for row in cursor.fetchall()
        ]
    return {
        "rule": {
            "id": record.id,
            "repository_id": record.repository_id,
            "status": record.status,
            "version": record.version,
            "title": record.title,
            "guidance": record.guidance,
            "applies_to": list(record.applies_to),
            "severity": record.severity,
            "category": record.category,
            "evidence_count": record.evidence_count,
        },
        "evidence": evidence,
        "history": history,
    }
