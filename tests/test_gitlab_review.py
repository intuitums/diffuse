import json
from urllib.parse import parse_qs

import httpx
import pytest

from service.github_review import format_review_body
from service.gitlab_review import (
    fetch_gitlab_merge_request_diff,
    fetch_gitlab_pull_request_update_diff,
    publish_gitlab_review,
)
from service.review_description import merge_review_description
from service.review_models import Category, ReviewFinding, ReviewReport, Severity
from service.scm import PullRequestEvent

DIFF = (
    "diff --git a/service/read.py b/service/read.py\n"
    "--- a/service/read.py\n"
    "+++ b/service/read.py\n"
    "@@ -11,2 +11,2 @@\n"
    " context\n"
    "-unsafe\n"
    "+safe\n"
)


def _event() -> PullRequestEvent:
    return PullRequestEvent.from_payload(
        {
            "provider": "gitlab",
            "scm_base_url": "https://gitlab.example.com",
            "api_base_url": "https://gitlab.example.com/api/v4",
            "repo_full_name": "group/subgroup/repo",
            "number": 17,
            "web_url": (
                "https://gitlab.example.com/group/subgroup/repo/-/merge_requests/17"
            ),
            "action": "opened",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "updated_at": "2026-07-23T15:30:00Z",
            "delivery_id": "delivery-17",
            "author": "contributor",
            "base_branch": "main",
            "head_branch": "feature/auth",
            "is_draft": False,
            "labels": [],
            "title": "Protect tenant reads",
            "description": "",
            "trigger_kind": "automatic",
            "trigger_id": "",
            "metadata_complete": True,
            "changed_file_count": 1,
            "state": "open",
            "source_created_at": "2026-07-23T14:00:00Z",
            "source_closed_at": "",
            "source_merged_at": "",
            "additions": 0,
            "deletions": 0,
            "source_project_id": 122,
        }
    )


def _report() -> ReviewReport:
    return ReviewReport(
        summary="One authorization issue was found.",
        risk_score=7,
        confidence_score=3,
        findings=[
            ReviewFinding(
                fingerprint="f" * 64,
                title="Validate tenant access",
                body="The changed code does not constrain the query by tenant.",
                severity=Severity.HIGH,
                category=Category.SECURITY,
                confidence=0.94,
                file_path="service/read.py",
                line=12,
                side="RIGHT",
                evidence="The new query filters only by object ID.",
                suggested_fix="Add the tenant identifier to the query.",
            )
        ],
        diff_file_count=1,
        reviewed_file_count=1,
        context_chunk_count=2,
        prompt_tokens=100,
        completion_tokens=20,
    )


@pytest.mark.anyio
async def test_fetch_merge_request_raw_diff_is_nested_project_safe(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    requests: list[httpx.Request] = []
    diff = (
        "diff --git a/service/read.py b/service/read.py\n"
        "--- a/service/read.py\n"
        "+++ b/service/read.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=diff)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await fetch_gitlab_merge_request_diff(_event(), client=client)

    assert value == diff
    assert requests[0].url.raw_path.startswith(
        b"/api/v4/projects/group%2Fsubgroup%2Frepo/"
        b"merge_requests/17/raw_diffs"
    )
    assert requests[0].headers["PRIVATE-TOKEN"] == "test-token"


@pytest.mark.anyio
async def test_update_diff_is_pinned_between_reviewed_heads(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "commit": {"id": "a" * 40},
                "compare_timeout": False,
                "diffs": [
                    {
                        "old_path": "service/read.py",
                        "new_path": "service/read.py",
                        "new_file": False,
                        "deleted_file": False,
                        "renamed_file": False,
                        "collapsed": False,
                        "too_large": False,
                        "diff": "@@ -1 +1 @@\n-old\n+new",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await fetch_gitlab_pull_request_update_diff(
            _event(),
            "c" * 40,
            client=client,
        )

    assert "diff --git a/service/read.py b/service/read.py" in value
    assert "@@ -1 +1 @@" in value
    assert requests[0].url.params["from"] == "c" * 40
    assert requests[0].url.params["to"] == "a" * 40
    assert requests[0].url.params["straight"] == "true"
    assert requests[0].url.path == "/api/v4/projects/122/repository/compare"


@pytest.mark.anyio
async def test_unavailable_force_pushed_comparison_fails_closed(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/repository/compare"):
            return httpx.Response(404, json={"message": "commit not found"})
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await fetch_gitlab_pull_request_update_diff(
            _event(),
            "c" * 40,
            client=client,
        )

    assert value == ""
    assert paths == ["/api/v4/projects/122/repository/compare"]


@pytest.mark.anyio
async def test_collapsed_comparison_fails_closed_for_continuity(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "commit": {"id": "a" * 40},
                "compare_timeout": False,
                "diffs": [
                    {
                        "old_path": "service/read.py",
                        "new_path": "service/read.py",
                        "new_file": False,
                        "deleted_file": False,
                        "renamed_file": False,
                        "collapsed": True,
                        "too_large": False,
                        "diff": "",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await fetch_gitlab_pull_request_update_diff(
            _event(),
            "c" * 40,
            client=client,
        )

    assert value == ""


@pytest.mark.anyio
async def test_publish_recovers_existing_summary_note_by_marker(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    marker = f"<!-- diffuse-review:42:{'a' * 40} -->"
    methods: list[str] = []
    finding_marker = f"<!-- diffuse-finding:{'f' * 64} -->"

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "discussion-1",
                        "notes": [
                            {
                                "id": 201,
                                "body": finding_marker,
                                "url": _event().web_url + "#note_201",
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": 301,
                    "body": marker + "\n<!-- diffuse-inline-comments:attached -->",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=42,
            report=_report(),
            diff_text=DIFF,
            client=client,
        )

    assert methods == ["GET", "GET"]
    assert published.external_id == "301"
    assert published.external_url == _event().web_url + "#note_301"
    assert published.inline_comments_attached
    assert published.finding_comments[0].external_id == "201"
    assert published.finding_comments[0].thread_id == "discussion-1"


@pytest.mark.anyio
async def test_publish_creates_revision_pinned_summary_with_findings(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    summary_payloads: list[dict] = []
    discussion_payloads: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/discussions"):
            discussion_payloads.append(parse_qs(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "id": "discussion-2",
                    "notes": [
                        {
                            "id": 202,
                            "body": discussion_payloads[-1]["body"][0],
                        }
                    ],
                },
            )
        summary_payloads.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={"id": 302, "body": summary_payloads[-1]["body"]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=42,
            report=_report(),
            diff_text=DIFF,
            client=client,
        )

    assert published.external_id == "302"
    assert published.inline_comments_attached
    assert published.finding_comments[0].thread_id == "discussion-2"
    assert len(discussion_payloads) == 1
    position = discussion_payloads[0]
    assert position["position[base_sha]"] == ["b" * 40]
    assert position["position[head_sha]"] == ["a" * 40]
    assert position["position[start_sha]"] == ["b" * 40]
    assert position["position[old_path]"] == ["service/read.py"]
    assert position["position[new_path]"] == ["service/read.py"]
    assert position["position[new_line]"] == ["12"]
    assert "position[old_line]" not in position
    assert summary_payloads[0]["merge_request_diff_head_sha"] == "a" * 40
    body = summary_payloads[0]["body"]
    assert "<!-- diffuse-inline-comments:attached -->" in body
    assert "The changed code does not constrain the query by tenant." not in body
    assert "/-/commit/" + ("a" * 40) in body
    assert "Reply `@diffuse review`" not in body


@pytest.mark.anyio
async def test_publish_updates_managed_merge_request_description(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    paths: list[tuple[str, str]] = []
    discussion_payloads: list[dict[str, list[str]]] = []
    description_payloads: list[dict] = []
    human_description = "Human-authored intent.\n\n- [ ] Preserve this checklist"

    def merge_request(description: str) -> dict:
        return {
            "iid": 17,
            "web_url": _event().web_url,
            "state": "opened",
            "sha": "a" * 40,
            "description": description,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/discussions"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            discussion_payloads.append(parse_qs(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "id": "discussion-description",
                    "notes": [
                        {
                            "id": 206,
                            "body": discussion_payloads[-1]["body"][0],
                        }
                    ],
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=merge_request(human_description))
        if request.method == "PUT":
            payload = json.loads(request.content)
            description_payloads.append(payload)
            return httpx.Response(
                200,
                json=merge_request(payload["description"]),
            )
        raise AssertionError(
            f"Unexpected request: {request.method} {request.url.path}"
        )

    report = _report().model_copy(
        update={
            "update_description": True,
            "fix_with_agent_enabled": False,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=42,
            report=report,
            diff_text=DIFF,
            client=client,
        )

    assert published.external_id == f"description:17:{'a' * 40}"
    assert all(not path.endswith("/notes") for _, path in paths)
    assert "get_fix_handoff" not in discussion_payloads[0]["body"][0]
    assert "Suggested fix" not in discussion_payloads[0]["body"][0]
    managed = description_payloads[0]["description"]
    assert managed.startswith(human_description)
    assert "<!-- diffuse-review-description:start -->" in managed
    assert "## Diffuse code review" in managed
    assert "<!-- diffuse-review-description:end -->" in managed


@pytest.mark.anyio
async def test_publish_retry_recovers_discussion_and_managed_description(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    report = _report().model_copy(update={"update_description": True})
    managed = merge_review_description(
        "Human context",
        format_review_body(
            42,
            "a" * 40,
            report,
            commit_url=(
                "https://gitlab.example.com/group/subgroup/repo/-/commit/"
                + ("a" * 40)
            ),
            visible_content=True,
            inline_fallback_message=(
                "GitLab could not attach every validated finding to its exact "
                "diff line, so the complete findings are included below."
            ),
            rerun_instruction="",
        ),
        max_chars=1_048_576,
    )
    methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "discussion-recovered",
                        "notes": [
                            {
                                "id": 207,
                                "body": "<!-- diffuse-finding:"
                                + ("f" * 64)
                                + " -->",
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "iid": 17,
                "web_url": _event().web_url,
                "state": "opened",
                "sha": "a" * 40,
                "description": managed,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=42,
            report=report,
            diff_text=DIFF,
            client=client,
        )

    assert published.external_id == f"description:17:{'a' * 40}"
    assert published.finding_comments[0].external_id == "207"
    assert methods == [
        (
            "GET",
            (
                "/api/v4/projects/group/subgroup/repo/"
                "merge_requests/17/discussions"
            ),
        ),
        (
            "GET",
            "/api/v4/projects/group/subgroup/repo/merge_requests/17",
        ),
    ]


@pytest.mark.anyio
async def test_publish_silent_gitlab_review_without_findings_makes_no_note(
    monkeypatch,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Silent review should not call GitLab")

    report = _report().model_copy(
        update={"findings": [], "summary_comment_enabled": False}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=42,
            report=report,
            client=client,
        )

    assert published.external_id == f"silent:17:{'a' * 40}"
    assert requests == []


@pytest.mark.anyio
async def test_gitlab_description_update_refuses_a_changed_head(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "iid": 17,
                "web_url": _event().web_url,
                "state": "opened",
                "sha": "c" * 40,
                "description": "Human context",
            },
        )

    report = _report().model_copy(
        update={"findings": [], "update_description": True}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="reviewed open revision"):
            await publish_gitlab_review(
                _event(),
                review_run_id=42,
                report=report,
                client=client,
            )

    assert methods == ["GET"]


@pytest.mark.anyio
async def test_uncommentable_finding_falls_back_to_complete_summary(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 303, "body": payloads[-1]["body"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=43,
            report=_report(),
            diff_text="",
            client=client,
        )

    assert not published.inline_comments_attached
    assert published.finding_comments == ()
    assert len(payloads) == 1
    assert "<!-- diffuse-inline-comments:fallback -->" in payloads[0]["body"]
    assert "The changed code does not constrain the query by tenant." in payloads[0]["body"]


@pytest.mark.anyio
async def test_removed_line_discussion_uses_only_old_line(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    finding = _report().findings[0].model_copy(
        update={
            "file_path": "service/removed.py",
            "line": 12,
            "side": "LEFT",
        }
    )
    report = _report().model_copy(update={"findings": [finding]})
    diff = (
        "diff --git a/service/removed.py b/service/removed.py\n"
        "--- a/service/removed.py\n"
        "+++ /dev/null\n"
        "@@ -11,2 +0,0 @@\n"
        "-context\n"
        "-unsafe\n"
    )
    discussion_payloads: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/discussions"):
            discussion_payloads.append(parse_qs(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "id": "discussion-left",
                    "notes": [
                        {
                            "id": 205,
                            "body": discussion_payloads[-1]["body"][0],
                        }
                    ],
                },
            )
        return httpx.Response(201, json={"id": 305})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await publish_gitlab_review(
            _event(),
            review_run_id=44,
            report=report,
            diff_text=diff,
            client=client,
        )

    assert published.inline_comments_attached
    position = discussion_payloads[0]
    assert position["position[old_line]"] == ["12"]
    assert "position[new_line]" not in position
    assert position["position[old_path]"] == ["service/removed.py"]
    assert position["position[new_path]"] == ["service/removed.py"]
