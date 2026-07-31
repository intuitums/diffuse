"""HTTP surface for reporting."""

from __future__ import annotations

from reports.db import connect
from reports.query import recent_invoices


def invoices_endpoint(request) -> dict:
    """GET /reports/invoices?sort=<column>&limit=<n>

    `sort` and `limit` come straight from the query string and are not
    validated here; the query layer is expected to constrain them.
    """
    tenant_id = request.session["tenant_id"]
    sort_column = request.query_params.get("sort", "created_at")
    limit = request.query_params.get("limit", "50")
    with connect() as connection:
        rows = recent_invoices(connection, tenant_id, sort_column, limit)
    return {"invoices": rows}
