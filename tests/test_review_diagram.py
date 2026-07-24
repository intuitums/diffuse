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


def test_unsafe_diagram_is_discarded_without_failing_the_review(monkeypatch):
    """The diagram is optional enrichment; rejecting it must not lose the review."""

    def reject(*_args, **_kwargs):
        raise ValidationError.from_exception_data("DiagramProposal", [])

    monkeypatch.setattr(review_engine, "_call_structured", reject)
    monkeypatch.setattr(review_engine, "_diagram_would_help", lambda _diff: True)

    diagram, prompt_tokens, completion_tokens = review_engine._generate_diagram(
        parse_unified_diff(
            "diff --git a/service/auth.py b/service/auth.py\n"
            "--- a/service/auth.py\n"
            "+++ b/service/auth.py\n"
            "@@ -1,1 +1,2 @@\n"
            "+return account\n"
        ),
        ["@@ -1,1 +1,2 @@\n+return account"],
        "",
        None,
    )

    assert diagram is None
    assert (prompt_tokens, completion_tokens) == (0, 0)
