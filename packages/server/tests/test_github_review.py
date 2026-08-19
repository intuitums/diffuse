import json
import re

import httpx
import pytest
from diffuse.github.review_publish import (
    _finding_comment,
    format_review_body,
    publish_github_review,
)
from diffuse.repository.scm import PullRequestEvent
from diffuse.review.description import merge_review_description
from diffuse.review.lineage import FindingSnapshot, ReviewContinuity
from diffuse_protocol.review import (
    Category,
    ReviewDiagram,
    ReviewFinding,
    ReviewReport,
    SecurityClassification,
    Severity,
)


def _event() -> PullRequestEvent:
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


def _report() -> ReviewReport:
    return ReviewReport(
        summary="One concrete issue affects @owners.",
        risk_score=7,
        confidence_score=2,
        findings=[
            ReviewFinding(
                fingerprint="f" * 64,
                title="Validate @user input",
                body="The changed line returns untrusted input.",
                severity=Severity.HIGH,
                category=Category.SECURITY,
                confidence=0.91,
                file_path="app.py",
                line=12,
                side="RIGHT",
                evidence="The new return bypasses validation.",
                suggested_fix="Call validate(value) first.",
            )
        ],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=2,
        prompt_tokens=100,
        completion_tokens=20,
    )


def test_review_body_renders_validated_diagram_with_section_presentation():
    report = _report().model_copy(
        update={
            "diagram": ReviewDiagram(
                kind="flow",
                title="Request <authorization> flow",
                mermaid="flowchart LR\n  Request --> Authorize --> Store",
            ),
            "diagram_collapsible": True,
            "diagram_default_open": False,
        }
    )

    body = format_review_body(
        42,
        "a" * 40,
        report,
        review_number=2,
    )

    assert "<details>" in body
    assert "<details open>" not in body
    assert "Request &lt;authorization&gt; flow" in body
    assert "```mermaid" in body
    assert report.diagram is not None
    assert report.diagram.mermaid in body


def test_review_output_does_not_advertise_agent_handoffs():
    report = _report()

    body = format_review_body(42, "a" * 40, report)
    inline = _finding_comment(report.findings[0])

    assert "get_fix_all_handoff" not in body
    assert "get_fix_handoff" not in body
    assert "Fix all with your agent" not in body
    assert "Fix with your agent" not in inline
    assert "get_fix_handoff" not in inline
    assert "**Suggested fix:**" in inline
    assert "Call validate(value) first." in inline


def test_review_output_can_hide_suggested_fix_guidance():
    report = _report()

    body = format_review_body(42, "a" * 40, report)
    inline = _finding_comment(
        report.findings[0],
        include_fix_guidance=False,
    )

    assert "get_fix_all_handoff" not in body
    assert "get_fix_handoff" not in inline
    assert "Suggested fix" not in inline
    assert "The changed line returns untrusted input." in inline
    assert inline.endswith(f"<!-- diffuse-finding:{'f' * 64} -->")


def test_suggested_fix_appears_below_actionable_content():
    report = _report()

    inline = _finding_comment(report.findings[0])

    assert "**Suggested fix:**" in inline
    assert inline.index("**Evidence:**") < inline.index("**Suggested fix:**")
    assert inline.endswith(f"<!-- diffuse-finding:{'f' * 64} -->")
    assert "get_fix_handoff" not in inline
    assert "Fix with your agent" not in inline


def _table_cell_count(row: str) -> int:
    return len(re.split(r"(?<!\\)\|", row)) - 2


def test_issues_table_rows_match_the_header_cell_count():
    report = _report()

    body = format_review_body(42, "a" * 40, report)
    header, delimiter, *rows = [
        line for line in body.splitlines() if line.startswith("|")
    ]

    assert _table_cell_count(header) == 4
    assert _table_cell_count(delimiter) == 4
    assert len(rows) == 1
    assert _table_cell_count(rows[0]) == 4
    assert "`app.py:12` |" in rows[0]
    assert rows[0].endswith(" 91% |")


def test_issues_table_rows_match_the_header_without_the_confidence_column():
    report = _report().model_copy(
        update={"confidence_score_section_included": False}
    )

    body = format_review_body(42, "a" * 40, report)
    header, delimiter, *rows = [
        line for line in body.splitlines() if line.startswith("|")
    ]

    assert _table_cell_count(header) == 3
    assert _table_cell_count(delimiter) == 3
    assert len(rows) == 1
    assert _table_cell_count(rows[0]) == 3
    assert rows[0].endswith("`app.py:12` |")
    assert "91%" not in rows[0]


def test_clean_review_body_states_only_the_verdict():
    report = _report().model_copy(
        update={
            "summary": "No issues found in the changed files.",
            "risk_score": 0,
            "findings": [],
        }
    )

    body = format_review_body(42, "a" * 40, report, inline_comments_attached=False)

    assert "No issues found in the changed files." in body
    assert "**Findings:** 0" in body
    assert "No inline findings were published" not in body
    assert "| Severity | Finding |" not in body
    assert "get_fix_all_handoff" not in body


def test_review_body_can_be_reduced_to_recovery_markers():
    report = _report().model_copy(update={"summary_comment_enabled": False})

    body = format_review_body(42, "a" * 40, report)

    assert body == (
        f"<!-- diffuse-review:42:{'a' * 40} -->\n"
        "<!-- diffuse-inline-comments:attached -->"
    )


def test_long_inline_finding_preserves_identity_marker():
    finding = _report().findings[0].model_copy(
        update={
            "body": "b" * 6000,
            "evidence": "e" * 3000,
            "suggested_fix": "s" * 6000,
        }
    )

    inline = _finding_comment(finding)

    assert len(inline) <= 10_000
    assert "get_fix_handoff" not in inline
    assert inline.endswith(f"<!-- diffuse-finding:{'f' * 64} -->")


def test_review_body_honors_output_visibility_without_hiding_fallback_findings():
    report = _report().model_copy(
        update={
            "summary_section_included": False,
            "issues_table_section_included": False,
            "confidence_score_section_included": False,
            "footer_included": False,
        }
    )

    body = format_review_body(
        42,
        "a" * 40,
        report,
        inline_comments_attached=False,
    )
    inline = _finding_comment(
        report.findings[0],
        include_confidence=False,
    )

    assert report.summary not in body
    assert "| Severity | Finding |" not in body
    assert "**Confidence:**" not in body
    assert "Last reviewed commit" not in body
    assert "**Risk:** 7.0/10" in body
    assert "The changed line returns untrusted input." in body
    assert "**Confidence:**" not in inline
    assert "**Category:** `security`" in inline


def test_review_body_collapses_configured_summary_issues_and_confidence():
    report = _report().model_copy(
        update={
            "summary_section_collapsible": True,
            "summary_section_default_open": False,
            "issues_table_section_collapsible": True,
            "issues_table_section_default_open": False,
            "confidence_score_section_collapsible": True,
            "confidence_score_section_default_open": True,
        }
    )

    body = format_review_body(42, "a" * 40, report)

    assert "<summary><strong>Summary</strong></summary>" in body
    assert "<summary><strong>Issues</strong></summary>" in body
    assert "<details open>" in body
    assert "<summary><strong>Confidence score</strong></summary>" in body


@pytest.mark.anyio
async def test_publish_recovers_existing_review_by_marker(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    marker = f"<!-- diffuse-review:42:{'a' * 40} -->"
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/reviews/99/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 109,
                        "node_id": "PRRC_109",
                        "html_url": "https://example/comment/109",
                        "body": "<!-- diffuse-finding:" + ("f" * 64) + " -->",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[{"id": 99, "html_url": "https://example/review/99", "body": marker}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=_report(),
            client=client,
        )

    assert methods == ["GET", "GET"]
    assert published.external_id == "99"
    assert published.external_url == "https://example/review/99"


@pytest.mark.anyio
async def test_publish_uses_exact_commit_and_inline_location(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/reviews/100/comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 110,
                            "node_id": "PRRC_110",
                            "html_url": "https://example/comment/110",
                            "body": "<!-- diffuse-finding:" + ("f" * 64) + " -->",
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": 100, "html_url": "https://example/review/100"},
        )

    event = _event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_github_review(
            event,
            review_run_id=42,
            report=_report(),
            review_number=3,
            client=client,
        )

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["commit_id"] == event.head_sha
    assert payload["event"] == "COMMENT"
    assert "**Confidence:** 2/5" in payload["body"]
    assert "Review 3" in payload["body"]
    assert (
        f"https://github.com/owner/repo/commit/{event.head_sha}"
        in payload["body"]
    )
    assert "Reply `@diffuse review` to re-run" in payload["body"]
    assert payload["comments"][0]["path"] == "app.py"
    assert payload["comments"][0]["line"] == 12
    assert payload["comments"][0]["side"] == "RIGHT"
    assert "🔒 Security vulnerability" in payload["comments"][0]["body"]
    assert "🔒 Security vulnerability" in payload["body"]
    assert "@\u200buser" in payload["comments"][0]["body"]
    assert "@\u200bowners" in payload["body"]


@pytest.mark.anyio
async def test_publish_updates_managed_pull_request_description(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    review_payloads: list[dict] = []
    description_payloads: list[dict] = []
    patch_headers: list[httpx.Headers] = []
    human_body = "Human-authored context.\n\n- [ ] Keep this checklist"

    def pull(body: str) -> dict:
        return {
            "number": 7,
            "html_url": _event().web_url,
            "state": "open",
            "head": {"sha": "a" * 40},
            "body": body,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/reviews") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/reviews") and request.method == "POST":
            review_payloads.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={"id": 106, "html_url": "https://example/review/106"},
            )
        if path.endswith("/reviews/106/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 116,
                        "body": "<!-- diffuse-finding:" + ("f" * 64) + " -->",
                    }
                ],
            )
        if path.endswith("/pulls/7") and request.method == "GET":
            return httpx.Response(
                200,
                headers={"ETag": '"revision-1"'},
                json=pull(human_body),
            )
        if path.endswith("/pulls/7") and request.method == "PATCH":
            patch_headers.append(request.headers)
            payload = json.loads(request.content)
            description_payloads.append(payload)
            return httpx.Response(200, json=pull(payload["body"]))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    report = _report().model_copy(update={"update_description": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    assert published.external_id == "106"
    assert review_payloads[0]["body"] == (
        f"<!-- diffuse-review:42:{'a' * 40} -->\n"
        "<!-- diffuse-inline-comments:attached -->"
    )
    managed = description_payloads[0]["body"]
    assert managed.startswith(human_body)
    assert "<!-- diffuse-review-description:start -->" in managed
    assert "## Diffuse code review" in managed
    assert "<!-- diffuse-review-description:end -->" in managed
    assert patch_headers[0]["If-Match"] == '"revision-1"'


@pytest.mark.anyio
async def test_publish_retry_recovers_review_and_managed_description(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    report = _report().model_copy(update={"update_description": True})
    managed = merge_review_description(
        "Human context",
        format_review_body(
            42,
            "a" * 40,
            report,
            commit_url=f"https://github.com/owner/repo/commit/{'a' * 40}",
            visible_content=True,
        ),
        max_chars=65_536,
    )
    methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 108,
                        "html_url": "https://example/review/108",
                        "body": (
                            f"<!-- diffuse-review:42:{'a' * 40} -->\n"
                            "<!-- diffuse-inline-comments:attached -->"
                        ),
                    }
                ],
            )
        if request.url.path.endswith("/reviews/108/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 118,
                        "body": "<!-- diffuse-finding:" + ("f" * 64) + " -->",
                    }
                ],
            )
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "html_url": _event().web_url,
                    "state": "open",
                    "head": {"sha": "a" * 40},
                    "body": managed,
                },
            )
        raise AssertionError(
            f"Unexpected request: {request.method} {request.url.path}"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    assert published.external_id == "108"
    assert methods == [
        ("GET", "/repos/owner/repo/pulls/7/reviews"),
        ("GET", "/repos/owner/repo/pulls/7/reviews/108/comments"),
        ("GET", "/repos/owner/repo/pulls/7"),
    ]


@pytest.mark.anyio
async def test_publish_suppresses_summary_but_keeps_inline_findings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/reviews/107/comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 117,
                            "body": "<!-- diffuse-finding:"
                            + ("f" * 64)
                            + " -->",
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 107})

    report = _report().model_copy(update={"summary_comment_enabled": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    assert published.external_id == "107"
    assert len(payloads[0]["comments"]) == 1
    assert "## Diffuse code review" not in payloads[0]["body"]
    assert "<!-- diffuse-review:42:" in payloads[0]["body"]


@pytest.mark.anyio
async def test_publish_silent_review_without_findings_avoids_empty_comment(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    report = _report().model_copy(
        update={"findings": [], "summary_comment_enabled": False}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    assert published.external_id == f"silent:7:{'a' * 40}"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/repos/owner/repo/pulls/7/reviews")
    ]


@pytest.mark.anyio
async def test_description_update_refuses_a_changed_head(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "number": 7,
                "html_url": _event().web_url,
                "state": "open",
                "head": {"sha": "c" * 40},
                "body": "Human context",
            },
        )

    report = _report().model_copy(
        update={"findings": [], "update_description": True}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="reviewed open revision"):
            await publish_github_review(
                _event(),
                review_run_id=42,
                report=report,
                client=client,
            )

    assert methods == ["GET", "GET"]


@pytest.mark.anyio
async def test_publish_labels_preventative_security_without_claiming_vulnerability(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []
    report = _report()
    finding = report.findings[0].model_copy(
        update={
            "severity": Severity.MEDIUM,
            "security_classification": SecurityClassification.PREVENTATIVE,
        }
    )
    report = report.model_copy(update={"findings": [finding], "risk_score": 4})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/reviews/105/comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 115,
                            "body": "<!-- diffuse-finding:"
                            + finding.fingerprint
                            + " -->",
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 105})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_github_review(
            _event(),
            review_run_id=43,
            report=report,
            client=client,
        )

    assert "🛡️ Preventative security risk" in payloads[0]["comments"][0]["body"]
    assert "🛡️ Preventative security risk" in payloads[0]["body"]


@pytest.mark.anyio
async def test_publish_falls_back_to_summary_when_inline_comments_are_rejected(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(422, json={"message": "invalid line"})
        return httpx.Response(200, json={"id": 101})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=_report(),
            client=client,
        )

    assert published.external_id == "101"
    assert len(payloads) == 2
    assert payloads[0]["comments"]
    assert payloads[1]["comments"] == []
    assert "GitHub could not attach the inline annotations" in payloads[1]["body"]
    assert "The changed line returns untrusted input." in payloads[1]["body"]
    # The summary fallback anchors nothing, so the lineage activation path must
    # be told which `new` findings lost their promised root thread.
    assert published.finding_comments == ()
    assert not published.inline_comments_attached
    assert published.unattached_fingerprints == ("f" * 64,)


@pytest.mark.anyio
async def test_publish_honors_summary_only_policy_without_inline_attempt(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 102})

    report = _report().model_copy(update={"inline_comments_enabled": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    # Policy never promised an inline thread, so activation is not withheld.
    assert published.unattached_fingerprints == ()
    assert len(payloads) == 1
    assert payloads[0]["comments"] == []
    assert "Repository policy requested summary-only review" in payloads[0]["body"]
    assert "The changed line returns untrusted input." in payloads[0]["body"]


@pytest.mark.anyio
async def test_publish_rejects_policy_disabled_report(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    report = _report().model_copy(update={"publication_enabled": False})

    with pytest.raises(ValueError, match="disabled publication"):
        await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
        )


@pytest.mark.anyio
async def test_continuity_publishes_only_new_inline_findings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    new_finding = _report().findings[0]
    persistent = new_finding.model_copy(
        update={
            "fingerprint": "e" * 64,
            "title": "Preserve the existing validation",
            "line": 20,
        }
    )
    report = _report().model_copy(
        update={"findings": [new_finding, persistent]}
    )
    continuity = ReviewContinuity(
        new_fingerprints=(new_finding.fingerprint,),
        persistent_fingerprints=(persistent.fingerprint,),
        addressed=(
            FindingSnapshot(
                lineage_id=7,
                title="Remove the stale bypass",
                severity="high",
                category="security",
                file_path="old.py",
                line=8,
                side="RIGHT",
            ),
        ),
        open_findings=(new_finding, persistent),
    )
    review_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/reviews/104/comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 114,
                            "node_id": "PRRC_114",
                            "body": (
                                "<!-- diffuse-finding:"
                                + new_finding.fingerprint
                                + " -->"
                            ),
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        review_payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 104})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_github_review(
            _event(),
            review_run_id=42,
            report=report,
            continuity=continuity,
            client=client,
        )

    assert len(review_payloads[0]["comments"]) == 1
    assert new_finding.fingerprint in review_payloads[0]["comments"][0]["body"]
    assert persistent.fingerprint not in review_payloads[0]["comments"][0]["body"]
    assert "1 new · 1 still open · 0 reopened · 1 addressed" in review_payloads[0]["body"]
    assert "Remove the stale bypass" in review_payloads[0]["body"]
    assert published.finding_comments[0].external_id == "114"


@pytest.mark.anyio
async def test_publish_fails_closed_when_review_dedupe_scan_hits_its_cap(
    monkeypatch,
):
    """A reviews list that never drops below a full page past the scan cap must
    abort publication instead of assuming no review exists.

    The marker search is the only dedupe guard before POSTing a new review:
    returning None after a capped scan can publish a second review onto a pull
    request that already carries one for this exact head.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {"id": i, "body": "unrelated review"}
                    for i in range(100)
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method}")

    report = _report().model_copy(
        update={"findings": [], "summary_comment_enabled": False}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="pagination cap"):
            await publish_github_review(
                _event(),
                review_run_id=42,
                report=report,
                client=client,
            )

    # 20 full pages were fetched and nothing was posted.
    assert len(requests) == 20
    assert all(method == "GET" for method, _ in requests)


@pytest.mark.anyio
async def test_publish_fails_closed_when_visibility_scan_hits_its_cap(monkeypatch):
    """A review-comment scan past its cap must abort even after the review POST.

    The scan decides which inline comments are already visible for this head;
    an incomplete scan must not be treated as ground truth for the next retry.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    posted = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.method == "POST" and request.url.path.endswith("/reviews"):
            posted += 1
            return httpx.Response(201, json={"id": 100})
        if request.url.path.endswith("/reviews/100/comments"):
            return httpx.Response(
                200,
                json=[
                    {"id": i, "body": "unrelated comment"}
                    for i in range(100)
                ],
            )
        if request.method == "GET" and request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {request.method}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="pagination cap"):
            await publish_github_review(
                _event(),
                review_run_id=42,
                report=_report(),
                client=client,
            )

    assert posted == 1
