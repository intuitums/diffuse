"""Durable operator-managed custom context.

Mutation APIs (create/update/delete) were part of the removed public MCP/REST
surface. Active contexts loaded for review still apply; operators manage rows
out of band until a supported v1 path lands.
"""

from __future__ import annotations

import psycopg2.extras

from repository_policy.resolve import ApprovedCustomContext


def load_active_custom_contexts(
    conn,
    *,
    repository_id: int,
) -> tuple[ApprovedCustomContext, ...]:
    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                context_type,
                body,
                applies_to,
                metadata
            FROM custom_contexts
            WHERE repository_id = %s
              AND status = 'active'
            ORDER BY id
            LIMIT 101
            """,
            (repository_id,),
        )
        rows = cursor.fetchall()
    if len(rows) > 100:
        raise ValueError("At most 100 active custom contexts may apply")
    return tuple(
        ApprovedCustomContext(
            id=int(row["id"]),
            context_type=row["context_type"],
            body=row["body"],
            applies_to=tuple(row["applies_to"]),
            metadata=dict(row["metadata"]),
        )
        for row in rows
    )
