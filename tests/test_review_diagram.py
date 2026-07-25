import pytest
from pydantic import ValidationError

from service import review_engine
from service.diff_parser import parse_unified_diff
from service.review_models import ReviewDiagram


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        (
            "sequence",
            "sequenceDiagram\n  API->>Store: load account",
        ),
        (
            "entity_relation",
            "erDiagram\n  ACCOUNT ||--o{ SESSION : owns",
        ),
        (
            "class",
            "classDiagram\n  Account <|-- PremiumAccount",
        ),
        (
            "flow",
            "flowchart LR\n  Request --> Authorize --> Store",
        ),
    ],
)
def test_review_diagram_accepts_supported_bounded_mermaid(kind, source):
    diagram = ReviewDiagram(
        kind=kind,
        title="Change relationships",
        mermaid=f"\n{source}\n",
    )

    assert diagram.mermaid == source


@pytest.mark.parametrize(
    "source",
    [
        "classDiagram\n  A <|-- B",
        "sequenceDiagram\n  click API https://attacker.invalid",
        "sequenceDiagram\n  %%{init: {'theme': 'dark'}}%%",
        "sequenceDiagram\n  API->>Store: https://attacker.invalid",
        "```mermaid\nsequenceDiagram\n```",
        "sequenceDiagram\n  classDef danger fill:red",
        "sequenceDiagram\n  API->>Store: <script>alert(1)</script>",
    ],
)
def test_review_diagram_rejects_mismatch_and_active_or_embedded_content(source):
    with pytest.raises(ValidationError):
        ReviewDiagram(
            kind="sequence",
            title="Unsafe diagram",
            mermaid=source,
        )


def _diagram_diff():
    return parse_unified_diff(
        "diff --git a/service/auth.py b/service/auth.py\n"
        "--- a/service/auth.py\n"
        "+++ b/service/auth.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+return account\n"
    )


def test_unsafe_diagram_is_discarded_without_failing_the_review(monkeypatch):
    """The diagram is optional enrichment; rejecting it must not lose the review."""

    def unsafe_diagram(**_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"diagram":{"kind":"sequence","title":"Unsafe",'
                            '"mermaid":"sequenceDiagram\\n'
                            '  click API https://attacker.invalid"}}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 17, "completion_tokens": 5},
        }

    monkeypatch.setenv("REVIEW_STRUCTURED_OUTPUT_MODE", "prompt")
    monkeypatch.setattr(review_engine.litellm, "completion", unsafe_diagram)
    monkeypatch.setattr(review_engine, "_diagram_would_help", lambda _diff: True)

    diagram, prompt_tokens, completion_tokens = review_engine._generate_diagram(
        _diagram_diff(),
        ["@@ -1,1 +1,2 @@\n+return account"],
        "",
        None,
    )

    assert diagram is None
    assert (prompt_tokens, completion_tokens) == (17, 5)


def test_empty_diagram_response_is_discarded_without_failing_the_review(monkeypatch):
    """Empty diagram content is a RuntimeError; it must not discard the review."""

    def empty_diagram(**_kwargs):
        return {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 0},
        }

    monkeypatch.setenv("REVIEW_STRUCTURED_OUTPUT_MODE", "prompt")
    monkeypatch.setattr(review_engine.litellm, "completion", empty_diagram)
    monkeypatch.setattr(review_engine, "_diagram_would_help", lambda _diff: True)

    diagram, prompt_tokens, completion_tokens = review_engine._generate_diagram(
        _diagram_diff(),
        ["@@ -1,1 +1,2 @@\n+return account"],
        "",
        None,
    )

    assert diagram is None
    assert (prompt_tokens, completion_tokens) == (9, 0)
