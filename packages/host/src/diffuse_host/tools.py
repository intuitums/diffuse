"""Minimal tool contract consumed by an isolated Agent Host."""

from __future__ import annotations

from typing import Protocol


class ReviewToolProvider(Protocol):
    """Searches the exact repository context granted to one investigation."""

    def search_code(
        self,
        query: str,
        *,
        path_prefix: str | None = None,
        limit: int = 8,
    ) -> dict[str, object]:
        """Return bounded code-search results for the current investigation."""
