"""Persist and load repository policy with its immutable index snapshot."""

from __future__ import annotations

import psycopg2.extras

from .models import (
    GuidanceDocument,
    PolicyLayer,
    RepositoryConfig,
    RepositoryPolicySnapshot,
)


def write_repository_policy(
    conn,
    snapshot_id: int,
    policy: RepositoryPolicySnapshot,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM index_snapshots WHERE id = %s FOR UPDATE",
            (snapshot_id,),
        )
        row = cursor.fetchone()
        if not row or row[0] != "building":
            raise ValueError("Repository policy can only be written to a building snapshot")

        cursor.execute(
            "DELETE FROM repository_policy_layers WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        cursor.execute(
            "DELETE FROM repository_guidance_documents WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        if policy.layers:
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO repository_policy_layers (
                    snapshot_id,
                    directory_path,
                    source_path,
                    config
                )
                VALUES %s
                """,
                [
                    (
                        snapshot_id,
                        layer.directory_path,
                        layer.source_path,
                        psycopg2.extras.Json(
                            layer.config.model_dump(mode="json")
                        ),
                    )
                    for layer in policy.layers
                ],
            )
        if policy.guidance_documents:
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO repository_guidance_documents (
                    snapshot_id,
                    directory_path,
                    source_path,
                    kind,
                    applies_to,
                    description,
                    content,
                    content_hash,
                    priority
                )
                VALUES %s
                """,
                [
                    (
                        snapshot_id,
                        document.directory_path,
                        document.source_path,
                        document.kind,
                        list(document.applies_to),
                        document.description,
                        document.content,
                        document.content_hash,
                        document.priority,
                    )
                    for document in policy.guidance_documents
                ],
            )
        cursor.execute(
            """
            UPDATE index_snapshots
            SET policy_fingerprint = %s,
                updated_at = now()
            WHERE id = %s
              AND status = 'building'
            """,
            (policy.fingerprint, snapshot_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Building snapshot disappeared while writing policy")


def load_repository_policy(conn, snapshot_id: int) -> RepositoryPolicySnapshot:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            "SELECT policy_fingerprint FROM index_snapshots WHERE id = %s",
            (snapshot_id,),
        )
        snapshot = cursor.fetchone()
        if not snapshot:
            raise ValueError("Index snapshot does not exist")
        cursor.execute(
            """
            SELECT directory_path, source_path, config
            FROM repository_policy_layers
            WHERE snapshot_id = %s
            ORDER BY
                array_length(string_to_array(directory_path, '/'), 1) NULLS FIRST,
                directory_path,
                source_path
            """,
            (snapshot_id,),
        )
        layers = tuple(
            PolicyLayer(
                directory_path=row["directory_path"],
                source_path=row["source_path"],
                config=RepositoryConfig.model_validate(row["config"]),
            )
            for row in cursor.fetchall()
        )
        cursor.execute(
            """
            SELECT
                directory_path,
                source_path,
                kind,
                applies_to,
                description,
                content,
                content_hash,
                priority
            FROM repository_guidance_documents
            WHERE snapshot_id = %s
            ORDER BY
                array_length(string_to_array(directory_path, '/'), 1) NULLS FIRST,
                directory_path,
                priority DESC,
                source_path,
                applies_to
            """,
            (snapshot_id,),
        )
        guidance = tuple(
            GuidanceDocument(
                directory_path=row["directory_path"],
                source_path=row["source_path"],
                kind=row["kind"],
                applies_to=tuple(row["applies_to"]),
                description=row["description"],
                content=row["content"],
                content_hash=row["content_hash"],
                priority=int(row["priority"]),
            )
            for row in cursor.fetchall()
        )
    return RepositoryPolicySnapshot(
        layers=layers,
        guidance_documents=guidance,
        fingerprint=snapshot["policy_fingerprint"],
    )
