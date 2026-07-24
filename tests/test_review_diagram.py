import pytest
from pydantic import ValidationError

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
