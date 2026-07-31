"""Managed review sections that preserve human-authored PR/MR descriptions."""

from __future__ import annotations

DESCRIPTION_START_MARKER = "<!-- diffuse-review-description:start -->"
DESCRIPTION_END_MARKER = "<!-- diffuse-review-description:end -->"


def _split_managed_description(
    value: str,
) -> tuple[str, str | None] | None:
    start_count = value.count(DESCRIPTION_START_MARKER)
    end_count = value.count(DESCRIPTION_END_MARKER)
    if start_count == end_count == 0:
        return value, None
    if start_count != 1 or end_count != 1:
        return None
    start = value.index(DESCRIPTION_START_MARKER)
    end = value.index(DESCRIPTION_END_MARKER)
    if end < start:
        return None
    end += len(DESCRIPTION_END_MARKER)
    return f"{value[:start]}{value[end:]}", value[start:end]


def is_managed_review_description_change(
    previous: object,
    current: object,
) -> bool:
    """Return true only when an edit changed Diffuse's region, not human text."""
    if previous is not None and not isinstance(previous, str):
        return False
    if current is not None and not isinstance(current, str):
        return False
    previous_parts = _split_managed_description(previous or "")
    current_parts = _split_managed_description(current or "")
    if previous_parts is None or current_parts is None:
        return False
    previous_human, previous_region = previous_parts
    current_human, current_region = current_parts
    regions = tuple(
        region
        for region in (previous_region, current_region)
        if region is not None
    )
    if not regions or any("<!-- diffuse-review:" not in region for region in regions):
        return False
    return previous_human.rstrip() == current_human.rstrip()


def merge_review_description(
    existing: str,
    review_body: str,
    *,
    max_chars: int,
) -> str:
    """Append or replace Diffuse's one reserved description region."""
    if not isinstance(existing, str):
        raise ValueError("Existing review description must be text")
    if not review_body:
        raise ValueError("Managed review description body cannot be empty")
    if max_chars <= 0:
        raise ValueError("Description size limit must be positive")
    if (
        DESCRIPTION_START_MARKER in review_body
        or DESCRIPTION_END_MARKER in review_body
    ):
        raise ValueError("Managed review body contains a reserved marker")

    start_count = existing.count(DESCRIPTION_START_MARKER)
    end_count = existing.count(DESCRIPTION_END_MARKER)
    if start_count != end_count or start_count > 1:
        raise RuntimeError(
            "PR/MR description contains ambiguous Diffuse review markers"
        )
    if (
        start_count == 1
        and existing.index(DESCRIPTION_END_MARKER)
        < existing.index(DESCRIPTION_START_MARKER)
    ):
        raise RuntimeError(
            "PR/MR description contains ambiguous Diffuse review markers"
        )
    section = (
        f"{DESCRIPTION_START_MARKER}\n"
        f"{review_body.rstrip()}\n"
        f"{DESCRIPTION_END_MARKER}"
    )
    if start_count == 1:
        start = existing.index(DESCRIPTION_START_MARKER)
        end = existing.index(DESCRIPTION_END_MARKER, start)
        end += len(DESCRIPTION_END_MARKER)
        merged = f"{existing[:start]}{section}{existing[end:]}"
    elif not existing:
        merged = section
    else:
        separator = "" if existing.endswith("\n\n") else "\n\n"
        merged = f"{existing}{separator}{section}"
    if len(merged) > max_chars:
        raise RuntimeError(
            "PR/MR description is too large to preserve while adding review output"
        )
    return merged
