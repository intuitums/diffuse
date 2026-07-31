"""Repository-authorized, deterministic review analytics projections."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg2.extras

from service.storage.mcp import McpRemote, resolve_mcp_repository

MAX_ANALYTICS_WINDOW_DAYS = 366


def _timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone")
    return parsed


def _window(start_at: str, end_at: str) -> tuple[datetime, datetime]:
    start = _timestamp(start_at, field="startAt").astimezone(UTC)
    end = _timestamp(end_at, field="endAt").astimezone(UTC)
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise ValueError("endAt must be later than startAt")
    if seconds > MAX_ANALYTICS_WINDOW_DAYS * 86400:
        raise ValueError(
            f"Analytics windows may span at most {MAX_ANALYTICS_WINDOW_DAYS} days"
        )
    return start, end


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator * 100 / denominator, 2)


def _author(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 255
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("author must contain 1 to 255 printable characters")
    return normalized


def get_review_analytics(
    conn,
    *,
    start_at: str,
    end_at: str,
    authorized_repository_ids: frozenset[int] | None = None,
    repository_id: int | None = None,
    repository_name: str | None = None,
    remote: McpRemote | None = None,
    default_branch: str | None = None,
    remote_url: str | None = None,
    author: str | None = None,
) -> dict[str, object]:
    """Aggregate exact durable review activity in a half-open time window."""
    start, end = _window(start_at, end_at)
    author = _author(author)
    descriptor = (repository_name, remote, default_branch)
    repository = None
    if repository_id is not None:
        if any(value is not None for value in (*descriptor, remote_url)):
            raise ValueError(
                "Use either repository_id or the repository descriptor, not both"
            )
        repository = resolve_mcp_repository(
            conn,
            authorized_repository_ids=authorized_repository_ids,
            repository_id=repository_id,
        )
        repository_ids: frozenset[int] | None = frozenset(
            {int(repository["id"])}
        )
    elif any(value is not None for value in (*descriptor, remote_url)):
        if any(value is None for value in descriptor):
            raise ValueError(
                "name, remote, and defaultBranch must be provided together"
            )
        repository = resolve_mcp_repository(
            conn,
            authorized_repository_ids=authorized_repository_ids,
            repository_name=repository_name,
            remote=remote,
            default_branch=default_branch,
            remote_url=remote_url,
        )
        repository_ids: frozenset[int] | None = frozenset(
            {int(repository["id"])}
        )
    else:
        repository_ids = authorized_repository_ids
    review_authorization = (
        "TRUE" if repository_ids is None else "review.repository_id = ANY(%s)"
    )
    review_parameters: list[object] = (
        [] if repository_ids is None else [sorted(repository_ids)]
    )
    if author is not None:
        review_authorization += (
            " AND EXISTS ("
            "SELECT 1 FROM pull_requests AS author_pull_request "
            "WHERE author_pull_request.id = review.pull_request_id "
            "AND lower(author_pull_request.author) = lower(%s)"
            ")"
        )
        review_parameters.append(author)
    parameters = [start, end, *review_parameters]
    pull_request_authorization = (
        "TRUE"
        if repository_ids is None
        else "pull_request.repository_id = ANY(%s)"
    )
    pull_request_parameters: list[object] = (
        [] if repository_ids is None else [sorted(repository_ids)]
    )
    if author is not None:
        pull_request_authorization += (
            " AND lower(pull_request.author) = lower(%s)"
        )
        pull_request_parameters.append(author)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH selected_reviews AS (
                SELECT review.*
                FROM review_runs AS review
                WHERE review.started_at >= %s
                  AND review.started_at < %s
                  AND {review_authorization}
            ),
            applied_findings AS (
                SELECT finding.*
                FROM review_findings AS finding
                JOIN selected_reviews AS review
                  ON review.id = finding.review_run_id
                 AND review.status = 'published'
                JOIN finding_lineage_events AS event
                  ON event.finding_id = finding.id
                 AND event.applied_at IS NOT NULL
            ),
            selected_lineages AS (
                SELECT DISTINCT lineage.*
                FROM finding_lineages AS lineage
                JOIN applied_findings AS finding
                  ON finding.lineage_id = lineage.id
            ),
            selected_threads AS (
                SELECT thread.id, thread.lineage_id
                FROM finding_threads AS thread
                JOIN selected_lineages AS lineage
                  ON lineage.id = thread.lineage_id
            ),
            current_reactions AS (
                SELECT DISTINCT ON (
                    feedback.repository_id,
                    feedback.source_external_id
                )
                    feedback.repository_id,
                    feedback.source_external_id,
                    feedback.finding_thread_id,
                    feedback.signal_kind,
                    feedback.event_action
                FROM review_feedback_events AS feedback
                JOIN selected_threads AS thread
                  ON thread.id = feedback.finding_thread_id
                WHERE feedback.source_kind = 'reaction'
                ORDER BY
                    feedback.repository_id,
                    feedback.source_external_id,
                    feedback.id DESC
            ),
            context_replies AS (
                SELECT DISTINCT
                    feedback.repository_id,
                    feedback.source_external_id,
                    feedback.finding_thread_id
                FROM review_feedback_events AS feedback
                JOIN selected_threads AS thread
                  ON thread.id = feedback.finding_thread_id
                WHERE feedback.source_kind = 'reply'
                  AND feedback.signal_kind = 'context'
                  AND feedback.event_action = 'observed'
            )
            SELECT
                transaction_timestamp() AS as_of,
                (SELECT count(*) FROM selected_reviews) AS review_attempts,
                (
                    SELECT count(DISTINCT pull_request_id)
                    FROM selected_reviews
                ) AS pull_requests_reviewed,
                (
                    SELECT count(*) FROM selected_reviews
                    WHERE status = 'published'
                ) AS published_reviews,
                (
                    SELECT count(*) FROM selected_reviews
                    WHERE status = 'failed'
                ) AS failed_reviews,
                (
                    SELECT count(*) FROM selected_reviews
                    WHERE status = 'skipped'
                ) AS skipped_reviews,
                (
                    SELECT count(*) FROM selected_reviews
                    WHERE status = 'superseded'
                ) AS superseded_reviews,
                (
                    SELECT count(*) FROM selected_reviews
                    WHERE status IN ('generating', 'ready', 'publishing')
                ) AS in_progress_reviews,
                (
                    SELECT avg(
                        extract(epoch FROM (published_at - started_at))
                    )
                    FROM selected_reviews
                    WHERE status = 'published'
                      AND published_at IS NOT NULL
                ) AS average_review_seconds,
                (
                    SELECT COALESCE(sum(prompt_tokens), 0)
                    FROM selected_reviews
                ) AS prompt_tokens,
                (
                    SELECT COALESCE(sum(completion_tokens), 0)
                    FROM selected_reviews
                ) AS completion_tokens,
                (SELECT count(*) FROM applied_findings) AS finding_occurrences,
                (SELECT count(*) FROM selected_lineages) AS unique_findings,
                (
                    SELECT count(*) FROM selected_lineages
                    WHERE status = 'addressed'
                ) AS addressed_findings,
                (
                    SELECT count(*) FROM selected_lineages
                    WHERE status = 'active'
                ) AS active_findings,
                (
                    SELECT count(*)
                    FROM selected_lineages AS lineage
                    JOIN LATERAL (
                        SELECT finding.severity, finding.category
                        FROM review_findings AS finding
                        JOIN finding_lineage_events AS event
                          ON event.finding_id = finding.id
                         AND event.applied_at IS NOT NULL
                        WHERE finding.lineage_id = lineage.id
                        ORDER BY event.id DESC
                        LIMIT 1
                    ) AS latest ON TRUE
                    WHERE lineage.status = 'active'
                      AND latest.severity = 'critical'
                ) AS active_critical_findings,
                (
                    SELECT count(*)
                    FROM selected_lineages AS lineage
                    JOIN LATERAL (
                        SELECT finding.category
                        FROM review_findings AS finding
                        JOIN finding_lineage_events AS event
                          ON event.finding_id = finding.id
                         AND event.applied_at IS NOT NULL
                        WHERE finding.lineage_id = lineage.id
                        ORDER BY event.id DESC
                        LIMIT 1
                    ) AS latest ON TRUE
                    WHERE lineage.status = 'active'
                      AND latest.category = 'security'
                ) AS active_security_findings,
                (
                    SELECT avg(
                        extract(epoch FROM (address_event.applied_at - lineage.created_at))
                    )
                    FROM selected_lineages AS lineage
                    JOIN LATERAL (
                        SELECT event.applied_at
                        FROM finding_lineage_events AS event
                        WHERE event.lineage_id = lineage.id
                          AND event.transition = 'addressed'
                          AND event.applied_at IS NOT NULL
                        ORDER BY event.applied_at
                        LIMIT 1
                    ) AS address_event ON TRUE
                ) AS average_address_seconds,
                (
                    SELECT count(DISTINCT snapshot.review_run_id)
                    FROM review_run_custom_contexts AS snapshot
                    JOIN selected_reviews AS review
                      ON review.id = snapshot.review_run_id
                    WHERE review.status = 'published'
                ) AS reviews_with_custom_context,
                (
                    SELECT count(*) FROM review_auto_approvals AS approval
                    JOIN selected_reviews AS review
                      ON review.id = approval.review_run_id
                    WHERE approval.status = 'published'
                ) AS auto_approvals,
                (
                    SELECT count(*)
                    FROM current_reactions
                    WHERE event_action = 'observed'
                      AND signal_kind = 'positive'
                ) AS positive_reactions,
                (
                    SELECT count(*)
                    FROM current_reactions
                    WHERE event_action = 'observed'
                      AND signal_kind = 'negative'
                ) AS negative_reactions,
                (
                    SELECT count(DISTINCT thread.lineage_id)
                    FROM current_reactions AS reaction
                    JOIN selected_threads AS thread
                      ON thread.id = reaction.finding_thread_id
                    WHERE reaction.event_action = 'observed'
                ) AS findings_with_reactions,
                (SELECT count(*) FROM context_replies) AS context_replies,
                (
                    SELECT count(DISTINCT thread.lineage_id)
                    FROM context_replies AS reply
                    JOIN selected_threads AS thread
                      ON thread.id = reply.finding_thread_id
                ) AS findings_with_context_replies
            """,
            parameters,
        )
        totals = dict(cursor.fetchone())

        cursor.execute(
            f"""
            WITH selected_findings AS (
                SELECT finding.*
                FROM review_findings AS finding
                JOIN review_runs AS review ON review.id = finding.review_run_id
                JOIN finding_lineage_events AS event
                  ON event.finding_id = finding.id
                 AND event.applied_at IS NOT NULL
                WHERE review.started_at >= %s
                  AND review.started_at < %s
                  AND review.status = 'published'
                  AND {review_authorization}
            )
            SELECT category, severity, count(*) AS count
            FROM selected_findings
            GROUP BY category, severity
            ORDER BY category, severity
            """,
            parameters,
        )
        finding_groups = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            SELECT
                repository.id AS repository_id,
                repository.full_name,
                count(*) AS review_attempts,
                count(*) FILTER (WHERE review.status = 'published')
                    AS published_reviews,
                count(DISTINCT review.pull_request_id) AS pull_requests_reviewed,
                COALESCE(sum(review.prompt_tokens + review.completion_tokens), 0)
                    AS total_tokens
            FROM review_runs AS review
            JOIN repositories AS repository ON repository.id = review.repository_id
            WHERE review.started_at >= %s
              AND review.started_at < %s
              AND {review_authorization}
            GROUP BY repository.id
            ORDER BY published_reviews DESC, repository.full_name
            """,
            parameters,
        )
        repositories = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            SELECT
                (
                    date_trunc('day', review.started_at AT TIME ZONE 'UTC')
                    AT TIME ZONE 'UTC'
                ) AS bucket_start,
                count(DISTINCT review.id) AS review_attempts,
                count(DISTINCT review.id) FILTER (
                    WHERE review.status = 'published'
                )
                    AS published_reviews,
                count(DISTINCT review.pull_request_id) FILTER (
                    WHERE review.status = 'published'
                ) AS pull_requests_reviewed,
                count(DISTINCT finding.id) FILTER (
                    WHERE event.applied_at IS NOT NULL
                      AND review.status = 'published'
                ) AS finding_occurrences
            FROM review_runs AS review
            LEFT JOIN review_findings AS finding
              ON finding.review_run_id = review.id
            LEFT JOIN finding_lineage_events AS event
              ON event.finding_id = finding.id
            WHERE review.started_at >= %s
              AND review.started_at < %s
              AND {review_authorization}
            GROUP BY bucket_start
            ORDER BY bucket_start
            """,
            parameters,
        )
        trend = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            WITH selected_lineages AS (
                SELECT DISTINCT finding.lineage_id
                FROM review_findings AS finding
                JOIN review_runs AS review
                  ON review.id = finding.review_run_id
                JOIN finding_lineage_events AS event
                  ON event.finding_id = finding.id
                 AND event.applied_at IS NOT NULL
                WHERE review.started_at >= %s
                  AND review.started_at < %s
                  AND review.status = 'published'
                  AND {review_authorization}
            )
            SELECT
                repository.full_name,
                repository.scm_provider,
                repository.scm_base_url,
                pull_request.number AS pull_request_number,
                pull_request.web_url AS pull_request_url,
                pull_request.title AS pull_request_title,
                lineage.id AS lineage_id,
                latest.review_run_id,
                latest.finding_id,
                latest.fingerprint,
                latest.title,
                latest.severity,
                latest.category,
                latest.security_classification,
                latest.file_path,
                latest.line,
                lineage.created_at
            FROM selected_lineages AS selected
            JOIN finding_lineages AS lineage
              ON lineage.id = selected.lineage_id
             AND lineage.status = 'active'
            JOIN pull_requests AS pull_request
              ON pull_request.id = lineage.pull_request_id
            JOIN repositories AS repository
              ON repository.id = pull_request.repository_id
            JOIN LATERAL (
                SELECT
                    finding.id AS finding_id,
                    finding.review_run_id,
                    finding.fingerprint,
                    finding.title,
                    finding.severity,
                    finding.category,
                    finding.security_classification,
                    finding.file_path,
                    finding.line
                FROM review_findings AS finding
                JOIN finding_lineage_events AS event
                  ON event.finding_id = finding.id
                 AND event.applied_at IS NOT NULL
                WHERE finding.lineage_id = lineage.id
                ORDER BY event.id DESC
                LIMIT 1
            ) AS latest ON TRUE
            ORDER BY
                CASE latest.severity
                    WHEN 'critical' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'medium' THEN 3
                    ELSE 4
                END,
                CASE WHEN latest.category = 'security' THEN 0 ELSE 1 END,
                repository.full_name,
                pull_request.number,
                latest.finding_id
            LIMIT 20
            """,
            parameters,
        )
        open_findings = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            WITH scoped_pull_requests AS (
                SELECT pull_request.*
                FROM pull_requests AS pull_request
                WHERE {pull_request_authorization}
            ),
            opened_pull_requests AS (
                SELECT pull_request.*
                FROM scoped_pull_requests AS pull_request
                WHERE pull_request.source_created_at >= %s
                  AND pull_request.source_created_at < %s
            ),
            reviewed_opened_pull_requests AS (
                SELECT DISTINCT pull_request.id
                FROM opened_pull_requests AS pull_request
                JOIN review_runs AS review
                  ON review.pull_request_id = pull_request.id
                WHERE review.started_at >= %s
                  AND review.started_at < %s
                  AND review.status = 'published'
            ),
            exact_merges AS (
                SELECT pull_request.*
                FROM scoped_pull_requests AS pull_request
                WHERE pull_request.source_merged_at >= %s
                  AND pull_request.source_merged_at < %s
                  AND pull_request.source_created_at IS NOT NULL
                  AND pull_request.source_merged_at
                      >= pull_request.source_created_at
            ),
            merge_events AS (
                SELECT
                    lifecycle.pull_request_id,
                    bool_or(lifecycle.source_merged_at IS NOT NULL)
                        AS has_exact_timestamp
                FROM pull_request_lifecycle_events AS lifecycle
                JOIN scoped_pull_requests AS pull_request
                  ON pull_request.id = lifecycle.pull_request_id
                WHERE lifecycle.state = 'merged'
                  AND lifecycle.source_event_at >= %s
                  AND lifecycle.source_event_at < %s
                GROUP BY lifecycle.pull_request_id
            )
            SELECT
                (SELECT count(*) FROM opened_pull_requests)
                    AS opened_pull_requests,
                (SELECT count(*) FROM reviewed_opened_pull_requests)
                    AS reviewed_opened_pull_requests,
                (
                    SELECT count(*) FROM opened_pull_requests
                    WHERE state = 'open'
                ) AS currently_open_from_cohort,
                (
                    SELECT count(*) FROM opened_pull_requests
                    WHERE state = 'closed'
                ) AS currently_closed_from_cohort,
                (
                    SELECT count(*) FROM opened_pull_requests
                    WHERE state = 'merged'
                ) AS currently_merged_from_cohort,
                (SELECT count(*) FROM exact_merges) AS exact_merges,
                (
                    SELECT avg(
                        extract(
                            epoch FROM (
                                source_merged_at - source_created_at
                            )
                        )
                    )
                    FROM exact_merges
                ) AS average_merge_seconds,
                (
                    SELECT percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY extract(
                            epoch FROM (
                                source_merged_at - source_created_at
                            )
                        )
                    )
                    FROM exact_merges
                ) AS median_merge_seconds,
                (SELECT count(*) FROM merge_events) AS merge_events,
                (
                    SELECT count(*) FROM merge_events
                    WHERE has_exact_timestamp
                ) AS merge_events_with_exact_timestamp
            """,
            (
                *pull_request_parameters,
                start,
                end,
                start,
                end,
                start,
                end,
                start,
                end,
            ),
        )
        pull_request_totals = dict(cursor.fetchone())

        cursor.execute(
            f"""
            WITH scoped_pull_requests AS (
                SELECT pull_request.*
                FROM pull_requests AS pull_request
                WHERE {pull_request_authorization}
            ),
            opened_daily AS (
                SELECT
                    (
                        date_trunc(
                            'day',
                            source_created_at AT TIME ZONE 'UTC'
                        ) AT TIME ZONE 'UTC'
                    ) AS bucket_start,
                    count(*) AS opened_pull_requests
                FROM scoped_pull_requests
                WHERE source_created_at >= %s
                  AND source_created_at < %s
                GROUP BY bucket_start
            ),
            merged_daily AS (
                SELECT
                    (
                        date_trunc(
                            'day',
                            source_merged_at AT TIME ZONE 'UTC'
                        ) AT TIME ZONE 'UTC'
                    ) AS bucket_start,
                    count(*) AS merged_pull_requests,
                    avg(
                        extract(
                            epoch FROM (
                                source_merged_at - source_created_at
                            )
                        )
                    ) AS average_merge_seconds
                FROM scoped_pull_requests
                WHERE source_merged_at >= %s
                  AND source_merged_at < %s
                  AND source_created_at IS NOT NULL
                  AND source_merged_at >= source_created_at
                GROUP BY bucket_start
            )
            SELECT
                COALESCE(opened.bucket_start, merged.bucket_start)
                    AS bucket_start,
                COALESCE(opened.opened_pull_requests, 0)
                    AS opened_pull_requests,
                COALESCE(merged.merged_pull_requests, 0)
                    AS merged_pull_requests,
                merged.average_merge_seconds
            FROM opened_daily AS opened
            FULL OUTER JOIN merged_daily AS merged
              ON merged.bucket_start = opened.bucket_start
            ORDER BY bucket_start
            """,
            (
                *pull_request_parameters,
                start,
                end,
                start,
                end,
            ),
        )
        pull_request_trend = [dict(row) for row in cursor.fetchall()]

    review_attempts = int(totals["review_attempts"])
    published_reviews = int(totals["published_reviews"])
    unique_findings = int(totals["unique_findings"])
    addressed_findings = int(totals["addressed_findings"])
    opened_pull_requests = int(pull_request_totals["opened_pull_requests"])
    reviewed_opened_pull_requests = int(
        pull_request_totals["reviewed_opened_pull_requests"]
    )
    merge_events = int(pull_request_totals["merge_events"])
    merge_events_with_exact_timestamp = int(
        pull_request_totals["merge_events_with_exact_timestamp"]
    )
    positive_reactions = int(totals["positive_reactions"])
    negative_reactions = int(totals["negative_reactions"])
    current_reactions = positive_reactions + negative_reactions
    trend_by_bucket: dict[datetime, dict[str, object]] = {}
    for row in trend:
        trend_by_bucket[row["bucket_start"]] = {
            "bucketStart": row["bucket_start"].isoformat(),
            "reviewAttempts": int(row["review_attempts"]),
            "publishedReviews": int(row["published_reviews"]),
            "pullRequestsReviewed": int(row["pull_requests_reviewed"]),
            "findingOccurrences": int(row["finding_occurrences"]),
            "openedPullRequests": 0,
            "mergedPullRequests": 0,
            "averageMergeSeconds": None,
        }
    for row in pull_request_trend:
        bucket = trend_by_bucket.setdefault(
            row["bucket_start"],
            {
                "bucketStart": row["bucket_start"].isoformat(),
                "reviewAttempts": 0,
                "publishedReviews": 0,
                "pullRequestsReviewed": 0,
                "findingOccurrences": 0,
                "openedPullRequests": 0,
                "mergedPullRequests": 0,
                "averageMergeSeconds": None,
            },
        )
        bucket["openedPullRequests"] = int(row["opened_pull_requests"])
        bucket["mergedPullRequests"] = int(row["merged_pull_requests"])
        bucket["averageMergeSeconds"] = (
            round(float(row["average_merge_seconds"]), 3)
            if row["average_merge_seconds"] is not None
            else None
        )
    return {
        "schemaVersion": "diffuse-review-analytics-v2",
        "asOf": totals["as_of"].isoformat(),
        "window": {
            "startAt": start.isoformat(),
            "endAt": end.isoformat(),
            "semantics": "half-open",
        },
        "repository": (
            {
                "id": int(repository["id"]),
                "name": repository["full_name"],
                "remote": repository["scm_provider"],
                "remoteUrl": repository["scm_base_url"],
                "defaultBranch": repository["default_branch"],
            }
            if repository
            else None
        ),
        "filters": {"author": author},
        "pullRequests": {
            "opened": opened_pull_requests,
            "openedReviewed": reviewed_opened_pull_requests,
            "openedUnreviewed": (
                opened_pull_requests - reviewed_opened_pull_requests
            ),
            "openedReviewCoverageRatePercent": _rate(
                reviewed_opened_pull_requests,
                opened_pull_requests,
            ),
            "currentlyOpenFromOpenedCohort": int(
                pull_request_totals["currently_open_from_cohort"]
            ),
            "currentlyClosedFromOpenedCohort": int(
                pull_request_totals["currently_closed_from_cohort"]
            ),
            "currentlyMergedFromOpenedCohort": int(
                pull_request_totals["currently_merged_from_cohort"]
            ),
            "merged": int(pull_request_totals["exact_merges"]),
            "averageMergeSeconds": (
                round(float(pull_request_totals["average_merge_seconds"]), 3)
                if pull_request_totals["average_merge_seconds"] is not None
                else None
            ),
            "medianMergeSeconds": (
                round(float(pull_request_totals["median_merge_seconds"]), 3)
                if pull_request_totals["median_merge_seconds"] is not None
                else None
            ),
            "mergeDurationSamples": int(pull_request_totals["exact_merges"]),
            "mergeEvents": merge_events,
            "mergeEventsWithExactTimestamp": (
                merge_events_with_exact_timestamp
            ),
            "mergeTimestampCompletenessRatePercent": _rate(
                merge_events_with_exact_timestamp,
                merge_events,
            ),
        },
        "reviews": {
            "attempts": review_attempts,
            "pullRequestsReviewed": int(totals["pull_requests_reviewed"]),
            "published": published_reviews,
            "failed": int(totals["failed_reviews"]),
            "skipped": int(totals["skipped_reviews"]),
            "superseded": int(totals["superseded_reviews"]),
            "inProgress": int(totals["in_progress_reviews"]),
            "completionRatePercent": _rate(
                published_reviews,
                review_attempts,
            ),
            "averageReviewSeconds": (
                round(float(totals["average_review_seconds"]), 3)
                if totals["average_review_seconds"] is not None
                else None
            ),
            "promptTokens": int(totals["prompt_tokens"]),
            "completionTokens": int(totals["completion_tokens"]),
            "autoApprovals": int(totals["auto_approvals"]),
            "withCustomContext": int(totals["reviews_with_custom_context"]),
            "customContextAdoptionRatePercent": _rate(
                int(totals["reviews_with_custom_context"]),
                published_reviews,
            ),
        },
        "findings": {
            "occurrences": int(totals["finding_occurrences"]),
            "unique": unique_findings,
            "currentlyAddressed": addressed_findings,
            "currentlyActive": int(totals["active_findings"]),
            "currentAddressRatePercent": _rate(
                addressed_findings,
                unique_findings,
            ),
            "activeCritical": int(totals["active_critical_findings"]),
            "activeSecurity": int(totals["active_security_findings"]),
            "averageAddressSeconds": (
                round(float(totals["average_address_seconds"]), 3)
                if totals["average_address_seconds"] is not None
                else None
            ),
            "byCategoryAndSeverity": [
                {
                    "category": row["category"],
                    "severity": row["severity"],
                    "count": int(row["count"]),
                }
                for row in finding_groups
            ],
            "open": [
                {
                    "lineageId": f"lineage_{row['lineage_id']}",
                    "findingId": f"finding_{row['finding_id']}",
                    "codeReviewId": f"review_{row['review_run_id']}",
                    "fingerprint": row["fingerprint"],
                    "title": row["title"],
                    "severity": row["severity"],
                    "category": row["category"],
                    "securityClassification": row[
                        "security_classification"
                    ],
                    "path": row["file_path"],
                    "line": int(row["line"]),
                    "firstSeenAt": row["created_at"].isoformat(),
                    "repository": {
                        "name": row["full_name"],
                        "remote": row["scm_provider"],
                        "remoteUrl": row["scm_base_url"],
                    },
                    "pullRequest": {
                        "number": int(row["pull_request_number"]),
                        "url": row["pull_request_url"],
                        "title": row["pull_request_title"],
                    },
                }
                for row in open_findings
            ],
            "openLimit": 20,
        },
        "engagement": {
            "currentPositiveReactions": positive_reactions,
            "currentNegativeReactions": negative_reactions,
            "currentReactions": current_reactions,
            "positiveReactionPercent": _rate(
                positive_reactions,
                current_reactions,
            ),
            "negativeReactionPercent": _rate(
                negative_reactions,
                current_reactions,
            ),
            "findingsWithCurrentReactions": int(
                totals["findings_with_reactions"]
            ),
            "reactionEngagementRatePercent": _rate(
                int(totals["findings_with_reactions"]),
                unique_findings,
            ),
            "contextReplies": int(totals["context_replies"]),
            "findingsWithContextReplies": int(
                totals["findings_with_context_replies"]
            ),
            "contextReplyRatePercent": _rate(
                int(totals["findings_with_context_replies"]),
                unique_findings,
            ),
        },
        "repositories": [
            {
                "id": int(row["repository_id"]),
                "name": row["full_name"],
                "reviewAttempts": int(row["review_attempts"]),
                "publishedReviews": int(row["published_reviews"]),
                "pullRequestsReviewed": int(row["pull_requests_reviewed"]),
                "totalTokens": int(row["total_tokens"]),
            }
            for row in repositories
        ],
        "dailyTrend": [
            trend_by_bucket[bucket] for bucket in sorted(trend_by_bucket)
        ],
        "definitions": {
            "openedReviewCoverageRatePercent": (
                "pull requests with an authoritative source-created timestamp "
                "in the window and at least one published review started in the "
                "same window / pull requests with an authoritative source-created "
                "timestamp in the window"
            ),
            "averageMergeSeconds": (
                "mean source-created-to-source-merged duration for pull requests "
                "whose authoritative merged timestamp is in the window"
            ),
            "medianMergeSeconds": (
                "median source-created-to-source-merged duration for pull requests "
                "whose authoritative merged timestamp is in the window"
            ),
            "mergeTimestampCompletenessRatePercent": (
                "distinct merged lifecycle events in the window carrying an "
                "authoritative merged timestamp / distinct merged lifecycle "
                "events in the window"
            ),
            "completionRatePercent": "published review runs / all runs started in window",
            "currentAddressRatePercent": (
                "currently addressed unique lineages / unique lineages with an "
                "applied finding occurrence in published runs started in window"
            ),
            "customContextAdoptionRatePercent": (
                "published runs with an operator-context snapshot / published runs"
            ),
            "reactionEngagementRatePercent": (
                "unique selected finding lineages with a currently observed "
                "positive or negative reaction / selected unique lineages"
            ),
            "positiveReactionPercent": (
                "currently observed positive reactions / all currently observed "
                "positive and negative reactions on selected finding lineages"
            ),
            "negativeReactionPercent": (
                "currently observed negative reactions / all currently observed "
                "positive and negative reactions on selected finding lineages"
            ),
            "contextReplyRatePercent": (
                "unique selected finding lineages with an observed context reply "
                "/ selected unique lineages"
            ),
            "findingSelection": (
                "applied finding occurrences in published review runs started in "
                "the reporting window; current state and engagement are projected "
                "from those selected lineages at query time"
            ),
        },
        "unavailableMetrics": [
            {
                "metric": "eligibleReviewCoverage",
                "reason": (
                    "opened reviewed-vs-unreviewed coverage is exact, but "
                    "historical review-policy eligibility is not yet versioned "
                    "for every observed pull request"
                ),
            },
            {
                "metric": "monetaryCost",
                "reason": (
                    "prompt and completion tokens are durable, but historical "
                    "provider pricing is not yet versioned"
                ),
            },
        ],
    }
