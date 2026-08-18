import pytest
from diffuse.review.description import (
    DESCRIPTION_END_MARKER,
    DESCRIPTION_START_MARKER,
    is_managed_review_description_change,
    merge_review_description,
)


def test_managed_description_preserves_human_text_and_is_idempotent():
    existing = "Human-authored purpose.\n\n## Checklist\n- [ ] Test"

    first = merge_review_description(
        existing,
        "## Diffuse code review\n\nFirst result",
        max_chars=10_000,
    )
    second = merge_review_description(
        first,
        "## Diffuse code review\n\nUpdated result",
        max_chars=10_000,
    )
    repeated = merge_review_description(
        second,
        "## Diffuse code review\n\nUpdated result",
        max_chars=10_000,
    )

    assert second.startswith(existing)
    assert "First result" not in second
    assert "Updated result" in second
    assert second.count(DESCRIPTION_START_MARKER) == 1
    assert second.count(DESCRIPTION_END_MARKER) == 1
    assert repeated == second


@pytest.mark.parametrize(
    "existing",
    [
        DESCRIPTION_START_MARKER,
        DESCRIPTION_END_MARKER,
        (
            f"{DESCRIPTION_END_MARKER}\ncontent\n"
            f"{DESCRIPTION_START_MARKER}"
        ),
        (
            f"{DESCRIPTION_START_MARKER}\n{DESCRIPTION_END_MARKER}\n"
            f"{DESCRIPTION_START_MARKER}\n{DESCRIPTION_END_MARKER}"
        ),
    ],
)
def test_managed_description_rejects_ambiguous_reserved_regions(existing: str):
    with pytest.raises(RuntimeError, match="ambiguous"):
        merge_review_description(existing, "review", max_chars=10_000)


def test_managed_description_refuses_to_truncate_human_text():
    with pytest.raises(RuntimeError, match="too large"):
        merge_review_description(
            "Human text",
            "review",
            max_chars=20,
        )


def test_managed_description_change_detects_bot_only_edits():
    previous = "Human purpose"
    current = merge_review_description(
        previous,
        "<!-- diffuse-review:42:abc -->\n## Review",
        max_chars=10_000,
    )
    updated = merge_review_description(
        current,
        "<!-- diffuse-review:43:def -->\n## Updated review",
        max_chars=10_000,
    )

    assert is_managed_review_description_change(previous, current)
    assert is_managed_review_description_change(current, updated)
    assert not is_managed_review_description_change(
        current,
        updated.replace("Human purpose", "Changed human purpose"),
    )
    assert not is_managed_review_description_change(
        previous,
        (
            f"{DESCRIPTION_START_MARKER}\nUntrusted content\n"
            f"{DESCRIPTION_END_MARKER}"
        ),
    )
