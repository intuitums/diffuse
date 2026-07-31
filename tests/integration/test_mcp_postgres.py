import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2
import psycopg2.extras
import pytest

from service.analytics_store import get_review_analytics
from service.custom_context_store import (
    create_custom_context,
    delete_custom_context,
    update_custom_context,
)
from service.mcp_actions import enqueue_mcp_review_trigger
from service.mcp_store import (
    get_mcp_code_review,
    get_mcp_custom_context,
    get_mcp_fix_all_handoff,
    get_mcp_fix_handoff,
    get_mcp_merge_request,
    get_mcp_review_trigger_target,
    list_mcp_code_reviews,
    list_mcp_custom_context,
    list_mcp_merge_request_comments,
    list_mcp_merge_requests,
    list_mcp_repositories,
    search_mcp_custom_context,
    search_mcp_review_comments,
)
from service.repositories import register_repository
from service.scm import PullRequestEvent
from service.workflow import enqueue_review_event


def test_mcp_read_projections_use_durable_review_lineage_and_context():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO repositories (
                    scm_provider,
                    scm_base_url,
                    full_name,
                    default_branch,
                    clone_url,
                    mirror_state,
                    last_fetched_sha
                )
                VALUES (
                    'github',
                    'https://github.example.com',
                    %s,
                    'main',
                    %s,
                    'ready',
                    %s
                )
                RETURNING id
                """,
                (
                    f"mcp/{suffix}",
                    f"https://github.example.com/mcp/{suffix}.git",
                    "a" * 40,
                ),
            )
            repository_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO index_snapshots (
                    repository_id,
                    commit_sha,
                    status,
                    index_format_version,
                    policy_fingerprint,
                    activated_at
                )
                VALUES (%s, %s, 'active', 'test-v1', %s, now())
                RETURNING id
                """,
                (repository_id, "a" * 40, "b" * 64),
            )
            snapshot_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO pull_requests (
                    repository_id,
                    number,
                    web_url,
                    base_sha,
                    head_sha,
                    author,
                    base_branch,
                    head_branch,
                    title,
                    description,
                    changed_file_count,
                    source_created_at,
                    latest_event_at
                )
                VALUES (
                    %s, 41, %s, %s, %s, 'developer', 'main', 'feature/mcp',
                    'Add MCP search', 'Adds durable finding search.', 1, now(), now()
                )
                RETURNING id
                """,
                (
                    repository_id,
                    f"https://github.example.com/mcp/{suffix}/pull/41",
                    "c" * 40,
                    "d" * 40,
                ),
            )
            pull_request_id = int(cursor.fetchone()[0])
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
                    status,
                    payload,
                    completed_at
                )
                VALUES (
                    %s, %s, 'review_pull_request', %s, %s, %s, %s,
                    'succeeded', %s, now()
                )
                RETURNING id
                """,
                (
                    repository_id,
                    pull_request_id,
                    f"mcp-review-{suffix}",
                    f"review:{repository_id}:41",
                    "c" * 40,
                    "d" * 40,
                    psycopg2.extras.Json({}),
                ),
            )
            workflow_job_id = int(cursor.fetchone()[0])
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
                    prompt_version,
                    context_fingerprint,
                    status,
                    summary,
                    risk_score,
                    confidence_score,
                    review_number,
                    context_chunk_count,
                    diff_file_count,
                    reviewed_file_count,
                    prompt_tokens,
                    completion_tokens,
                    ready_at,
                    published_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'test/review', 'mcp-test-v1',
                    %s, 'published', 'Validation is missing.', 6.5, 3, 1,
                    2, 1, 1, 20, 4, now(), now()
                )
                RETURNING id
                """,
                (
                    workflow_job_id,
                    repository_id,
                    pull_request_id,
                    snapshot_id,
                    "c" * 40,
                    "d" * 40,
                    "e" * 64,
                ),
            )
            review_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO finding_lineages (
                    pull_request_id,
                    initial_fingerprint,
                    status,
                    first_seen_review_run_id,
                    last_seen_review_run_id
                )
                VALUES (%s, %s, 'active', %s, %s)
                RETURNING id
                """,
                (pull_request_id, "f" * 64, review_id, review_id),
            )
            lineage_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO review_findings (
                    review_run_id,
                    lineage_id,
                    fingerprint,
                    ordinal,
                    title,
                    body,
                    severity,
                    category,
                    confidence,
                    file_path,
                    line,
                    side,
                    evidence,
                    suggested_fix
                )
                VALUES (
                    %s, %s, %s, 0, 'Validate the request',
                    'Caller input reaches the lookup without validation.',
                    'high', 'correctness', 0.91, 'service/api.py', 42, 'RIGHT',
                    'The new line passes request.id directly.',
                    'Validate request.id before the lookup.'
                )
                RETURNING id
                """,
                (review_id, lineage_id, "f" * 64),
            )
            finding_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO finding_lineage_events (
                    lineage_id,
                    review_run_id,
                    finding_id,
                    transition,
                    applied_at
                )
                VALUES (%s, %s, %s, 'new', now())
                """,
                (lineage_id, review_id, finding_id),
            )
            cursor.execute(
                """
                INSERT INTO finding_threads (
                    lineage_id,
                    scm_provider,
                    root_comment_id,
                    root_comment_node_id,
                    status,
                    created_review_run_id
                )
                VALUES (%s, 'github', 'github-comment-456', 'PRRC_456', 'active', %s)
                RETURNING id
                """,
                (lineage_id, review_id),
            )
            finding_thread_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO finding_lineages (
                    pull_request_id,
                    initial_fingerprint,
                    status,
                    first_seen_review_run_id,
                    last_seen_review_run_id
                )
                VALUES (%s, %s, 'active', %s, %s)
                RETURNING id
                """,
                (pull_request_id, "2" * 64, review_id, review_id),
            )
            security_lineage_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO review_findings (
                    review_run_id,
                    lineage_id,
                    fingerprint,
                    ordinal,
                    title,
                    body,
                    severity,
                    category,
                    security_classification,
                    confidence,
                    file_path,
                    line,
                    side,
                    evidence
                )
                VALUES (
                    %s, %s, %s, 1, 'Enforce tenant ownership',
                    'A cross-tenant object can be loaded by identifier.',
                    'critical', 'security', 'vulnerability', 0.97,
                    'service/auth.py', 18, 'RIGHT',
                    'The query omits the authenticated tenant identifier.'
                )
                RETURNING id
                """,
                (review_id, security_lineage_id, "2" * 64),
            )
            security_finding_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO finding_lineage_events (
                    lineage_id,
                    review_run_id,
                    finding_id,
                    transition,
                    applied_at
                )
                VALUES (%s, %s, %s, 'new', now())
                """,
                (security_lineage_id, review_id, security_finding_id),
            )
            cursor.execute(
                """
                INSERT INTO review_feedback_events (
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    scm_provider,
                    source_kind,
                    signal_kind,
                    event_action,
                    event_key,
                    source_external_id,
                    source_comment_id,
                    actor_login,
                    content,
                    finding_category,
                    finding_severity
                )
                VALUES
                    (
                        %s, %s, %s, %s, 'github', 'reaction', 'positive',
                        'observed', %s, %s, 'github-comment-456', 'reviewer',
                        '+1', 'correctness', 'high'
                    ),
                    (
                        %s, %s, %s, %s, 'github', 'reaction', 'negative',
                        'observed', %s, %s, 'github-comment-456', 'reviewer',
                        '-1', 'correctness', 'high'
                    ),
                    (
                        %s, %s, %s, %s, 'github', 'reaction', 'negative',
                        'withdrawn', %s, %s, 'github-comment-456', 'reviewer',
                        '-1', 'correctness', 'high'
                    ),
                    (
                        %s, %s, %s, %s, 'github', 'reply', 'context',
                        'observed', %s, %s, 'github-comment-456', 'reviewer',
                        'This endpoint is tenant scoped.', 'correctness', 'high'
                    )
                """,
                (
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    f"analytics-positive-{suffix}",
                    f"reaction-positive-{suffix}",
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    f"analytics-negative-add-{suffix}",
                    f"reaction-negative-{suffix}",
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    f"analytics-negative-remove-{suffix}",
                    f"reaction-negative-{suffix}",
                    repository_id,
                    pull_request_id,
                    finding_thread_id,
                    finding_id,
                    f"analytics-context-{suffix}",
                    f"reply-context-{suffix}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO learned_rules (
                    repository_id,
                    deduplication_key,
                    status,
                    title,
                    guidance,
                    applies_to,
                    severity,
                    category,
                    evidence_count,
                    activated_at
                )
                VALUES (
                    %s, %s, 'active', 'Validate identifiers',
                    'Validate caller-controlled identifiers before lookup.',
                    ARRAY['service/**'], 'high', 'correctness', 3, now()
                )
                RETURNING id
                """,
                (repository_id, "1" * 64),
            )
            learned_rule_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO learned_rule_events (
                    learned_rule_id,
                    action,
                    event_key,
                    rule_version,
                    snapshot
                )
                VALUES (%s, 'proposed', %s, 1, %s)
                """,
                (
                    learned_rule_id,
                    f"mcp-rule-{suffix}",
                    psycopg2.extras.Json(
                        {
                            "title": "Validate identifiers",
                            "guidance": (
                                "Validate caller-controlled identifiers before lookup."
                            ),
                        }
                    ),
                ),
            )

        manual_context = create_custom_context(
            connection,
            repository_id=repository_id,
            context_type="PATTERN",
            body="Always scope account lookups to the authenticated tenant.",
            applies_to=("service/**",),
            status="active",
            metadata={"source": "operator"},
            actor_kind="operator",
            actor_label="integration-test",
            actor_token_id=None,
            authorized_repository_ids=frozenset({repository_id}),
        )
        with connection.cursor() as cursor:
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
                    review_id,
                    int(str(manual_context["id"]).removeprefix("custom_context_")),
                    psycopg2.extras.Json(
                        {
                            "body": (
                                "Always scope account lookups to the authenticated "
                                "tenant."
                            ),
                            "status": "active",
                        }
                    ),
                ),
            )
        analytics = get_review_analytics(
            connection,
            start_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            end_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            authorized_repository_ids=frozenset({repository_id}),
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            author="developer",
        )
        unauthorized_analytics = get_review_analytics(
            connection,
            start_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            end_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            authorized_repository_ids=frozenset(),
        )
        repositories = list_mcp_repositories(connection)
        merge_requests = list_mcp_merge_requests(
            connection,
            repository_id=repository_id,
            state="open",
        )
        compatible_merge_requests = list_mcp_merge_requests(
            connection,
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com/",
            state="open",
        )
        merge_request = get_mcp_merge_request(
            connection,
            repository_id=repository_id,
            pull_request_number=41,
        )
        compatible_merge_request = get_mcp_merge_request(
            connection,
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            pull_request_number=41,
        )
        reviews = list_mcp_code_reviews(
            connection,
            repository_id=repository_id,
            status="COMPLETED",
        )
        compatible_reviews = list_mcp_code_reviews(
            connection,
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            pull_request_number=41,
            status="COMPLETED",
        )
        review = get_mcp_code_review(
            connection,
            code_review_id=f"review_{review_id}",
        )
        fix_handoff = get_mcp_fix_handoff(
            connection,
            code_review_id=f"review_{review_id}",
            finding_fingerprint="f" * 64,
            agent="codex",
        )
        fix_all_handoff = get_mcp_fix_all_handoff(
            connection,
            code_review_id=f"review_{review_id}",
            agent="conductor",
        )
        comments = list_mcp_merge_request_comments(
            connection,
            repository_id=repository_id,
            pull_request_number=41,
            addressed=False,
        )
        compatible_comments = list_mcp_merge_request_comments(
            connection,
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            pull_request_number=41,
            generated=True,
            addressed=False,
        )
        non_generated_comments = list_mcp_merge_request_comments(
            connection,
            repository_name=f"mcp/{suffix}",
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            pull_request_number=41,
            generated=False,
        )
        search = search_mcp_review_comments(
            connection,
            query="validation",
            repository_id=repository_id,
        )
        contexts = list_mcp_custom_context(
            connection,
            repository_id=repository_id,
            status="active",
            generated=True,
        )
        context = get_mcp_custom_context(
            connection,
            custom_context_id=f"learned_rule_{learned_rule_id}",
        )
        manual_context_detail = get_mcp_custom_context(
            connection,
            custom_context_id=str(manual_context["id"]),
        )
        updated_context = update_custom_context(
            connection,
            custom_context_id=str(manual_context["id"]),
            expected_updated_at=str(manual_context["updatedAt"]),
            context_type=None,
            body=(
                "Always scope account lookups to the authenticated tenant and "
                "reject cross-tenant identifiers."
            ),
            applies_to=("service/**", "api/**"),
            status="inactive",
            metadata={"source": "operator", "ticket": "SEC-41"},
            actor_kind="operator",
            actor_label="integration-test",
            actor_token_id=None,
            authorized_repository_ids=frozenset({repository_id}),
        )
        no_op_update = update_custom_context(
            connection,
            custom_context_id=str(manual_context["id"]),
            expected_updated_at=str(
                updated_context["customContext"]["updatedAt"]
            ),
            status="inactive",
            actor_kind="operator",
            actor_label="integration-test",
            actor_token_id=None,
            authorized_repository_ids=frozenset({repository_id}),
        )
        updated_context_detail = get_mcp_custom_context(
            connection,
            custom_context_id=str(manual_context["id"]),
        )
        context_search = search_mcp_custom_context(
            connection,
            query="authenticated tenant",
            repository_id=repository_id,
        )
        restricted_repositories = list_mcp_repositories(
            connection,
            authorized_repository_ids=frozenset({repository_id}),
        )
        restricted_reviews = list_mcp_code_reviews(
            connection,
            authorized_repository_ids=frozenset({repository_id}),
        )
        empty_search = search_mcp_review_comments(
            connection,
            query="validation",
            authorized_repository_ids=frozenset(),
        )

        matched_repository = next(
            item
            for item in repositories["repositories"]
            if item["id"] == repository_id
        )
        assert matched_repository["activeSnapshot"]["id"] == snapshot_id
        assert merge_requests["total"] == 1
        assert merge_requests["mergeRequests"][0]["number"] == 41
        assert compatible_merge_requests == merge_requests
        assert merge_request["mergeRequest"]["state"] == "open"
        assert merge_request["mergeRequest"]["reviewsCount"] == 1
        assert merge_request["mergeRequest"]["commentsCount"] == 2
        assert compatible_merge_request == merge_request
        assert reviews["total"] == 1
        assert compatible_reviews == reviews
        assert reviews["codeReviews"][0]["id"] == f"review_{review_id}"
        assert reviews["codeReviews"][0]["status"] == "COMPLETED"
        assert review["codeReview"]["riskScore"] == 6.5
        assert review["codeReview"]["findings"][0]["lineageId"] == (
            f"lineage_{lineage_id}"
        )
        assert fix_handoff["handoff"]["mode"] == "one"
        assert fix_handoff["handoff"]["requestedAgent"] == "codex"
        assert fix_handoff["handoff"]["pullRequest"]["headSha"] == "d" * 40
        assert fix_handoff["handoff"]["findings"][0]["id"] == (
            f"finding_{finding_id}"
        )
        assert "<diffuse_fix_handoff>" in fix_handoff["handoff"]["prompt"]
        assert fix_all_handoff["handoff"]["mode"] == "all"
        assert fix_all_handoff["handoff"]["requestedAgent"] == "conductor"
        assert len(fix_all_handoff["handoff"]["findings"]) == 2
        assert analytics["repository"]["id"] == repository_id
        assert analytics["filters"]["author"] == "developer"
        assert analytics["pullRequests"]["opened"] == 1
        assert analytics["pullRequests"]["openedReviewed"] == 1
        assert analytics["pullRequests"]["openedUnreviewed"] == 0
        assert (
            analytics["pullRequests"]["openedReviewCoverageRatePercent"]
            == 100.0
        )
        assert analytics["reviews"]["attempts"] == 1
        assert analytics["reviews"]["published"] == 1
        assert analytics["reviews"]["pullRequestsReviewed"] == 1
        assert analytics["reviews"]["completionRatePercent"] == 100.0
        assert analytics["reviews"]["promptTokens"] == 20
        assert analytics["reviews"]["completionTokens"] == 4
        assert analytics["reviews"]["withCustomContext"] == 1
        assert analytics["reviews"]["customContextAdoptionRatePercent"] == 100.0
        assert analytics["findings"]["occurrences"] == 2
        assert analytics["findings"]["unique"] == 2
        assert analytics["findings"]["currentlyActive"] == 2
        assert analytics["findings"]["activeCritical"] == 1
        assert analytics["findings"]["activeSecurity"] == 1
        assert len(analytics["findings"]["open"]) == 2
        assert analytics["findings"]["open"][0]["findingId"] == (
            f"finding_{security_finding_id}"
        )
        assert analytics["engagement"]["currentPositiveReactions"] == 1
        assert analytics["engagement"]["currentNegativeReactions"] == 0
        assert analytics["engagement"]["findingsWithCurrentReactions"] == 1
        assert analytics["engagement"]["reactionEngagementRatePercent"] == 50.0
        assert analytics["engagement"]["contextReplies"] == 1
        assert analytics["dailyTrend"][0]["reviewAttempts"] == 1
        assert analytics["dailyTrend"][0]["findingOccurrences"] == 2
        assert unauthorized_analytics["reviews"]["attempts"] == 0
        assert unauthorized_analytics["repositories"] == []
        assert unauthorized_analytics["findings"]["open"] == []
        assert comments["total"] == 2
        validation_comment = next(
            item
            for item in comments["comments"]
            if item["id"] == f"finding_{finding_id}"
        )
        assert not validation_comment["addressed"]
        assert validation_comment["commentId"] == "github-comment-456"
        assert validation_comment["diffuseGenerated"]
        assert compatible_comments == comments
        assert non_generated_comments["total"] == 0
        assert search["total"] == 1
        assert search["comments"][0]["filePath"] == "service/api.py"
        assert contexts["total"] == 1
        assert contexts["customContexts"][0]["status"] == "ACTIVE"
        assert context["customContext"]["id"] == (
            f"learned_rule_{learned_rule_id}"
        )
        assert context["customContext"]["history"][0]["action"] == "proposed"
        assert manual_context_detail["customContext"]["type"] == "PATTERN"
        assert manual_context_detail["customContext"]["history"][0]["action"] == (
            "custom_context.created"
        )
        assert updated_context["changed"]
        assert updated_context["customContext"]["status"] == "INACTIVE"
        assert updated_context["customContext"]["scopes"]["AND"][1]["value"] == (
            "api/**"
        )
        assert not no_op_update["changed"]
        assert no_op_update["customContext"]["updatedAt"] == (
            updated_context["customContext"]["updatedAt"]
        )
        assert [
            event["action"]
            for event in updated_context_detail["customContext"]["history"]
        ] == ["custom_context.created", "custom_context.updated"]
        assert context_search["total"] == 1
        assert context_search["customContexts"][0]["id"] == manual_context["id"]
        assert [
            item["id"] for item in restricted_repositories["repositories"]
        ] == [repository_id]
        assert restricted_reviews["total"] == 1
        assert empty_search["total"] == 0
        with pytest.raises(ValueError, match="authorized"):
            get_mcp_code_review(
                connection,
                code_review_id=f"review_{review_id}",
                authorized_repository_ids=frozenset(),
            )
        with pytest.raises(ValueError, match="authorized"):
            get_mcp_fix_all_handoff(
                connection,
                code_review_id=f"review_{review_id}",
                authorized_repository_ids=frozenset(),
            )
        with pytest.raises(ValueError, match="Agent target"):
            get_mcp_fix_all_handoff(
                connection,
                code_review_id=f"review_{review_id}",
                agent="unknown",
            )
        with pytest.raises(ValueError, match="authorized"):
            get_mcp_custom_context(
                connection,
                custom_context_id=f"learned_rule_{learned_rule_id}",
                authorized_repository_ids=frozenset(),
            )
        with pytest.raises(ValueError, match="changed since"):
            update_custom_context(
                connection,
                custom_context_id=str(manual_context["id"]),
                expected_updated_at=str(manual_context["updatedAt"]),
                status="active",
                actor_kind="operator",
                actor_label="integration-test",
                actor_token_id=None,
                authorized_repository_ids=frozenset({repository_id}),
            )
        with pytest.raises(ValueError, match="authorized"):
            delete_custom_context(
                connection,
                custom_context_id=str(manual_context["id"]),
                expected_updated_at=str(
                    updated_context["customContext"]["updatedAt"]
                ),
                actor_kind="operator",
                actor_label="integration-test",
                actor_token_id=None,
                authorized_repository_ids=frozenset(),
            )
        with pytest.raises(ValueError, match="authorized"):
            get_mcp_merge_request(
                connection,
                repository_id=repository_id,
                pull_request_number=41,
                authorized_repository_ids=frozenset(),
            )
        with pytest.raises(ValueError, match="provided together"):
            list_mcp_merge_requests(
                connection,
                repository_name=f"mcp/{suffix}",
                remote="github",
            )
        with pytest.raises(ValueError, match="authorized"):
            list_mcp_merge_requests(
                connection,
                repository_name=f"mcp/{suffix}",
                remote="github",
                default_branch="main",
                remote_url="https://github.example.com",
                authorized_repository_ids=frozenset(),
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pull_requests SET head_sha = %s WHERE id = %s",
                ("9" * 40, pull_request_id),
            )
        with pytest.raises(ValueError, match="not current"):
            get_mcp_fix_all_handoff(
                connection,
                code_review_id=f"review_{review_id}",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pull_requests SET head_sha = %s WHERE id = %s",
                ("d" * 40, pull_request_id),
            )

        manual_time = datetime.now(UTC) + timedelta(minutes=1)
        trigger_event = PullRequestEvent(
            provider="github",
            scm_base_url="https://github.example.com",
            api_base_url="https://api.github.example.com",
            repo_full_name=f"mcp/{suffix}",
            number=41,
            web_url=f"https://github.example.com/mcp/{suffix}/pull/41",
            action="manual",
            head_sha="d" * 40,
            base_sha="c" * 40,
            updated_at=manual_time.isoformat(),
            delivery_id=f"mcp-trigger-{suffix}",
            author="developer",
            base_branch="main",
            head_branch="feature/mcp",
            title="Add MCP search",
            description="Adds durable finding search.",
            trigger_kind="manual",
            trigger_id=f"mcp:{suffix}",
            metadata_complete=True,
            changed_file_count=1,
            state="open",
            source_created_at=(manual_time - timedelta(days=1)).isoformat(),
            additions=8,
            deletions=2,
        )
        trigger = enqueue_mcp_review_trigger(
            connection,
            event=trigger_event,
            repository_id=repository_id,
            authorized_repository_ids=frozenset({repository_id}),
            actor_kind="operator",
            actor_label="integration-test",
            actor_token_id=None,
        )
        assert trigger["success"]
        assert trigger["jobId"] is not None
        deletion = delete_custom_context(
            connection,
            custom_context_id=str(manual_context["id"]),
            expected_updated_at=str(
                updated_context["customContext"]["updatedAt"]
            ),
            actor_kind="operator",
            actor_label="integration-test",
            actor_token_id=None,
            authorized_repository_ids=frozenset({repository_id}),
        )
        assert deletion["deleted"]
        with pytest.raises(ValueError, match="does not exist"):
            get_mcp_custom_context(
                connection,
                custom_context_id=str(manual_context["id"]),
            )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM audit_events
                WHERE action IN (
                    'custom_context.created',
                    'code_review.triggered'
                )
                  AND repository_id = %s
                """,
                (repository_id,),
            )
            assert cursor.fetchone()[0] == 2
            cursor.execute(
                """
                SELECT action, details
                FROM audit_events
                WHERE resource_kind = 'custom_context'
                  AND resource_id = %s
                ORDER BY id
                """,
                (str(manual_context["id"]).removeprefix("custom_context_"),),
            )
            context_events = cursor.fetchall()
            assert [row[0] for row in context_events] == [
                "custom_context.created",
                "custom_context.updated",
                "custom_context.deleted",
            ]
            assert context_events[1][1]["changed_fields"] == [
                "body",
                "applies_to",
                "status",
                "metadata",
            ]
            assert len(context_events[2][1]["body_sha256"]) == 64
    finally:
        connection.rollback()
        connection.close()


def test_closed_pull_request_state_is_durable_and_cancels_queued_review():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex
    try:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.example.com",
            full_name=f"lifecycle/{suffix}",
            default_branch="main",
        )
        opened_at = datetime.now(UTC)

        def event(
            *,
            action: str,
            state: str,
            delivery: str,
            at: datetime,
            number: int = 9,
            exact_lifecycle: bool = True,
        ):
            return PullRequestEvent(
                provider="github",
                scm_base_url="https://github.example.com",
                api_base_url="https://api.github.example.com",
                repo_full_name=repository.full_name,
                number=number,
                web_url=(
                    f"https://github.example.com/{repository.full_name}/pull/{number}"
                ),
                action=action,
                head_sha="a" * 40,
                base_sha="b" * 40,
                updated_at=at.isoformat(),
                delivery_id=delivery,
                author="developer",
                base_branch="main",
                head_branch="feature/lifecycle",
                title="Exercise lifecycle",
                description="Persists merged state.",
                trigger_kind="automatic",
                metadata_complete=True,
                changed_file_count=1,
                state=state,
                source_created_at=opened_at.isoformat(),
                source_closed_at=(
                    at.isoformat()
                    if exact_lifecycle and state in {"closed", "merged"}
                    else ""
                ),
                source_merged_at=(
                    at.isoformat()
                    if exact_lifecycle and state == "merged"
                    else ""
                ),
                additions=4,
                deletions=1,
            )

        def enqueue(value: PullRequestEvent):
            payload = json.dumps(
                value.to_payload(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return enqueue_review_event(
                connection,
                value,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
            )

        opened = enqueue(
            event(
                action="opened",
                state="open",
                delivery=f"opened-{suffix}",
                at=opened_at,
            )
        )
        closed = enqueue(
            event(
                action="closed",
                state="closed",
                delivery=f"closed-unmerged-{suffix}",
                at=opened_at + timedelta(minutes=1),
            )
        )
        reopened = enqueue(
            event(
                action="reopened",
                state="open",
                delivery=f"reopened-{suffix}",
                at=opened_at + timedelta(minutes=2),
            )
        )
        merged = enqueue(
            event(
                action="closed",
                state="merged",
                delivery=f"closed-{suffix}",
                at=opened_at + timedelta(minutes=3),
            )
        )
        missing_timestamp_opened = enqueue(
            event(
                action="opened",
                state="open",
                delivery=f"missing-opened-{suffix}",
                at=opened_at + timedelta(seconds=10),
                number=10,
            )
        )
        missing_timestamp_merged = enqueue(
            event(
                action="closed",
                state="merged",
                delivery=f"missing-merged-{suffix}",
                at=opened_at + timedelta(minutes=4),
                number=10,
                exact_lifecycle=False,
            )
        )

        assert opened.accepted
        assert closed.state == "pull_request_closed"
        assert reopened.accepted
        assert merged.job_id is None
        assert merged.state == "pull_request_merged"
        assert missing_timestamp_opened.accepted
        assert missing_timestamp_merged.state == "pull_request_merged"
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, source_created_at, source_closed_at, source_merged_at
                FROM pull_requests
                WHERE repository_id = %s AND number = 9
                """,
                (repository.id,),
            )
            lifecycle_state = cursor.fetchone()
            assert lifecycle_state == (
                "merged",
                opened_at,
                opened_at + timedelta(minutes=3),
                opened_at + timedelta(minutes=3),
            )
            cursor.execute(
                "SELECT status FROM workflow_jobs WHERE id = %s",
                (opened.job_id,),
            )
            assert cursor.fetchone()[0] == "cancelled"
            cursor.execute(
                "SELECT status FROM workflow_jobs WHERE id = %s",
                (reopened.job_id,),
            )
            assert cursor.fetchone()[0] == "cancelled"
            cursor.execute(
                """
                SELECT action, state, source_event_at
                FROM pull_request_lifecycle_events
                WHERE pull_request_id = (
                    SELECT id
                    FROM pull_requests
                    WHERE repository_id = %s AND number = 9
                )
                ORDER BY source_event_at, id
                """,
                (repository.id,),
            )
            assert cursor.fetchall() == [
                ("opened", "open", opened_at),
                ("closed", "closed", opened_at + timedelta(minutes=1)),
                ("reopened", "open", opened_at + timedelta(minutes=2)),
                ("closed", "merged", opened_at + timedelta(minutes=3)),
            ]
        listed = list_mcp_merge_requests(
            connection,
            authorized_repository_ids=frozenset({repository.id}),
            state="merged",
        )
        assert listed["total"] == 2
        exact_merge = next(
            item for item in listed["mergeRequests"] if item["number"] == 9
        )
        missing_merge = next(
            item for item in listed["mergeRequests"] if item["number"] == 10
        )
        assert exact_merge["state"] == "merged"
        assert exact_merge["createdAt"] == opened_at.isoformat()
        assert exact_merge["mergedAt"] == (
            opened_at + timedelta(minutes=3)
        ).isoformat()
        assert missing_merge["mergedAt"] is None
        analytics = get_review_analytics(
            connection,
            start_at=(opened_at - timedelta(seconds=1)).isoformat(),
            end_at=(opened_at + timedelta(minutes=5)).isoformat(),
            authorized_repository_ids=frozenset({repository.id}),
            repository_name=repository.full_name,
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            author="developer",
        )
        assert analytics["pullRequests"]["opened"] == 2
        assert analytics["pullRequests"]["openedReviewed"] == 0
        assert analytics["pullRequests"]["openedUnreviewed"] == 2
        assert analytics["pullRequests"]["openedReviewCoverageRatePercent"] == 0.0
        assert analytics["pullRequests"]["currentlyMergedFromOpenedCohort"] == 2
        assert analytics["pullRequests"]["merged"] == 1
        assert analytics["pullRequests"]["averageMergeSeconds"] == 180.0
        assert analytics["pullRequests"]["medianMergeSeconds"] == 180.0
        assert analytics["pullRequests"]["mergeDurationSamples"] == 1
        assert analytics["pullRequests"]["mergeEvents"] == 2
        assert analytics["pullRequests"]["mergeEventsWithExactTimestamp"] == 1
        assert (
            analytics["pullRequests"]["mergeTimestampCompletenessRatePercent"]
            == 50.0
        )
    finally:
        connection.rollback()
        connection.close()


def test_mcp_trigger_is_authorized_audited_and_durable():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex
    try:
        repository = register_repository(
            connection,
            scm_provider="github",
            scm_base_url="https://github.example.com",
            full_name=f"trigger/{suffix}",
            default_branch="main",
        )
        opened_at = datetime.now(UTC)
        common = {
            "provider": "github",
            "scm_base_url": "https://github.example.com",
            "api_base_url": "https://api.github.example.com",
            "repo_full_name": repository.full_name,
            "number": 17,
            "web_url": (
                f"https://github.example.com/{repository.full_name}/pull/17"
            ),
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "author": "developer",
            "base_branch": "main",
            "head_branch": "feature/mcp",
            "title": "Add MCP review trigger",
            "description": "Queues an authorized manual review.",
            "metadata_complete": True,
            "changed_file_count": 2,
            "state": "open",
            "source_created_at": opened_at.isoformat(),
        }
        opened = PullRequestEvent(
            **common,
            action="opened",
            updated_at=opened_at.isoformat(),
            delivery_id=f"opened-{suffix}",
        )
        serialized = json.dumps(
            opened.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        initial = enqueue_review_event(
            connection,
            opened,
            payload_sha256=hashlib.sha256(serialized).hexdigest(),
        )
        assert initial.accepted

        target = get_mcp_review_trigger_target(
            connection,
            repository_name=repository.full_name,
            remote="github",
            default_branch="main",
            remote_url="https://github.example.com",
            pull_request_number=17,
            authorized_repository_ids=frozenset({repository.id}),
        )
        assert target["repositoryId"] == repository.id
        assert target["remote"] == "github"
        assert target["headBranch"] == "feature/mcp"

        manual = PullRequestEvent(
            **common,
            action="manual",
            updated_at=(opened_at + timedelta(minutes=1)).isoformat(),
            delivery_id=f"mcp-{suffix}",
            trigger_kind="manual",
            trigger_id=f"mcp:{suffix}",
        )
        result = enqueue_mcp_review_trigger(
            connection,
            event=manual,
            repository_id=repository.id,
            authorized_repository_ids=frozenset({repository.id}),
            actor_kind="service_token",
            actor_label="integration-agent",
            actor_token_id=7,
        )

        assert result["success"]
        assert result["repository"]["remote"] == "github"
        assert result["queueState"] == "queued"
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM workflow_jobs
                WHERE id = %s
                """,
                (result["jobId"],),
            )
            payload = cursor.fetchone()[0]
            assert payload["provider"] == "github"
            assert payload["action"] == "manual"
            assert payload["trigger_id"] == f"mcp:{suffix}"
            cursor.execute(
                """
                SELECT actor_kind, actor_label, action, details
                FROM audit_events
                WHERE repository_id = %s
                  AND action = 'code_review.triggered'
                """,
                (repository.id,),
            )
            audit = cursor.fetchone()
            assert audit[0:3] == (
                "service_token",
                "integration-agent",
                "code_review.triggered",
            )
            assert audit[3]["pull_request_number"] == 17
            assert audit[3]["head_sha"] == "a" * 40
            assert audit[3]["job_id"] == result["jobId"]
    finally:
        connection.rollback()
        connection.close()
