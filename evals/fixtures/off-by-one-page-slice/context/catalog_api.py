"""HTTP surface for the product catalog."""

from __future__ import annotations

from catalog.pagination import Page, paginate
from catalog.store import all_product_names


def list_products(offset: int, limit: int) -> dict:
    """Return one page of product names plus a cursor for the next page."""
    page: Page = paginate(all_product_names(), offset, limit)
    return {
        "items": page.items,
        "next_offset": page.offset + len(page.items) if page.has_more else None,
        "total": page.total,
    }
