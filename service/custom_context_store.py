"""Durable operator-managed custom context."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

import psycopg2.extras

from repository_policy.models import validate_repo_glob
from repository_policy.resolve import ApprovedCustomContext

CustomContextType = Literal["CUSTOM_INSTRUCTION", "PATTERN"]
CustomContextStatus = Literal["active", "inactive", "suggested"]

CUSTOM_CONTEXT_TYPES = frozenset({"CUSTOM_INSTRUCTION", "PATTERN"})
CUSTOM_CONTEXT_STATUSES = frozenset({"active", "inactive", "suggested"})
MAX_CUSTOM_CONTEXT_BODY_CHARS = 12_000
MAX_CUSTOM_CONTEXT_METADATA_BYTES = 16_384


def _actor(value: str) -> str:
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= 255
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("Custom-context actor is invalid")
    return normalized


def _metadata(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Custom-context metadata must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Custom-context metadata must be JSON serializable") from error
    if len(encoded) > MAX_CUSTOM_CONTEXT_METADATA_BYTES:
        raise ValueError(
            f"Custom-context metadata exceeds {MAX_CUSTOM_CONTEXT_METADATA_BYTES} bytes"
        )
    return value


def _body(value: str) -> str:
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= MAX_CUSTOM_CONTEXT_BODY_CHARS
        or "\x00" in normalized
    ):
        raise ValueError(
            f"Custom-context body must contain 1 to "
            f"{MAX_CUSTOM_CONTEXT_BODY_CHARS} characters"
        )
    return normalized


def _applies_to(value: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(value) <= 32:
        raise ValueError("Custom context requires 1 to 32 path scopes")
    normalized = tuple(dict.fromkeys(validate_repo_glob(item) for item in value))
    if not normalized:
        raise ValueError("Custom context requires 1 to 32 path scopes")
    return normalized


def _resource_id(value: str) -> int:
    prefix = "custom_context_"
    if not value.startswith(prefix):
        raise ValueError(
            "Only custom_context_<id> operator context can be mutated"
        )
    raw = value[len(prefix) :]
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("customContextId must use custom_context_<id> format")
    return int(raw)


def _expected_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("expectedUpdatedAt must be an ISO-8601 timestamp with timezone")
    return parsed


def _json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validate_actor(
    *,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
) -> str:
    normalized_actor = _actor(actor_label)
    if actor_kind not in {"operator", "service_token"}:
        raise ValueError("Custom-context actor kind is invalid")
    if actor_kind == "service_token":
        if actor_token_id is None or actor_token_id <= 0:
            raise ValueError("Custom-context service-token actor is invalid")
    elif actor_token_id is not None:
        raise ValueError("Operator custom-context actor cannot have a token ID")
    return normalized_actor


def custom_context_json(row: dict) -> dict[str, object]:
    return {
        "id": f"custom_context_{int(row['id'])}",
        "type": row["context_type"],
        "body": row["body"],
        "status": row["status"].upper(),
        "scopes": {
            "AND": [
                {
                    "operator": "MATCHES",
                    "field": "filepath",
                    "value": pattern,
                }
                for pattern in row["applies_to"]
            ]
        },
        "metadata": dict(row["metadata"]),
        "greptileGenerated": False,
        "diffuseGenerated": False,
        "evidenceCount": 0,
        "repository": {
            "id": int(row["repository_id"]),
            "name": row["repository_full_name"],
            "remote": row["scm_provider"],
            "remoteUrl": row["scm_base_url"],
        },
        "createdBy": row["created_by"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


def create_custom_context(
    conn,
    *,
    repository_id: int,
    context_type: CustomContextType,
    body: str,
    applies_to: tuple[str, ...],
    status: CustomContextStatus,
    metadata: dict[str, object],
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
    authorized_repository_ids: frozenset[int] | None,
) -> dict[str, object]:
    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    if (
        authorized_repository_ids is not None
        and repository_id not in authorized_repository_ids
    ):
        raise ValueError("Repository does not exist or is not authorized")
    if context_type not in CUSTOM_CONTEXT_TYPES:
        raise ValueError("Custom-context type is invalid")
    normalized_body = _body(body)
    normalized_applies_to = _applies_to(applies_to)
    if status not in CUSTOM_CONTEXT_STATUSES:
        raise ValueError("Custom-context status is invalid")
    normalized_metadata = _metadata(metadata)
    normalized_actor = _validate_actor(
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id
            FROM repositories
            WHERE id = %s
              AND enabled = TRUE
            FOR SHARE
            """,
            (repository_id,),
        )
        if cursor.fetchone() is None:
            raise ValueError("Repository does not exist or is not authorized")
        cursor.execute(
            """
            INSERT INTO custom_contexts (
                repository_id,
                context_type,
                body,
                status,
                applies_to,
                metadata,
                created_by_token_id,
                created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                repository_id,
                context_type,
                normalized_body,
                status,
                list(normalized_applies_to),
                psycopg2.extras.Json(normalized_metadata),
                actor_token_id,
                normalized_actor,
            ),
        )
        custom_context_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO audit_events (
                actor_kind,
                actor_label,
                action,
                resource_kind,
                resource_id,
                repository_id,
                details
            )
            VALUES (%s, %s, 'custom_context.created', 'custom_context', %s, %s, %s)
            """,
            (
                actor_kind,
                normalized_actor,
                str(custom_context_id),
                repository_id,
                psycopg2.extras.Json(
                    {
                        "context_type": context_type,
                        "status": status,
                        "applies_to": list(normalized_applies_to),
                    }
                ),
            ),
        )
        cursor.execute(
            """
            SELECT
                context.*,
                repository.full_name AS repository_full_name,
                repository.scm_provider,
                repository.scm_base_url
            FROM custom_contexts AS context
            JOIN repositories AS repository ON repository.id = context.repository_id
            WHERE context.id = %s
            """,
            (custom_context_id,),
        )
        return custom_context_json(dict(cursor.fetchone()))


def update_custom_context(
    conn,
    *,
    custom_context_id: str,
    expected_updated_at: str,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
    authorized_repository_ids: frozenset[int] | None,
    context_type: CustomContextType | None = None,
    body: str | None = None,
    applies_to: tuple[str, ...] | None = None,
    status: CustomContextStatus | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    context_id = _resource_id(custom_context_id)
    expected_timestamp = _expected_timestamp(expected_updated_at)
    if all(
        value is None
        for value in (context_type, body, applies_to, status, metadata)
    ):
        raise ValueError("At least one custom-context field must be updated")
    normalized_actor = _validate_actor(
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )
    authorization = "TRUE"
    authorization_parameters: list[object] = []
    if authorized_repository_ids is not None:
        authorization = "context.repository_id = ANY(%s)"
        authorization_parameters.append(sorted(authorized_repository_ids))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                context.*,
                repository.full_name AS repository_full_name,
                repository.scm_provider,
                repository.scm_base_url
            FROM custom_contexts AS context
            JOIN repositories AS repository ON repository.id = context.repository_id
            WHERE context.id = %s
              AND {authorization}
            FOR UPDATE OF context
            """,
            (context_id, *authorization_parameters),
        )
        current = cursor.fetchone()
        if current is None:
            raise ValueError("Custom context does not exist or is not authorized")
        if current["updated_at"] != expected_timestamp:
            raise ValueError("Custom context changed since expectedUpdatedAt")

        next_type = current["context_type"] if context_type is None else context_type
        if next_type not in CUSTOM_CONTEXT_TYPES:
            raise ValueError("Custom-context type is invalid")
        next_body = current["body"] if body is None else _body(body)
        next_applies_to = (
            tuple(current["applies_to"])
            if applies_to is None
            else _applies_to(applies_to)
        )
        next_status = current["status"] if status is None else status
        if next_status not in CUSTOM_CONTEXT_STATUSES:
            raise ValueError("Custom-context status is invalid")
        next_metadata = (
            dict(current["metadata"])
            if metadata is None
            else _metadata(metadata)
        )
        changes = {
            "context_type": current["context_type"] != next_type,
            "body": current["body"] != next_body,
            "applies_to": tuple(current["applies_to"]) != next_applies_to,
            "status": current["status"] != next_status,
            "metadata": dict(current["metadata"]) != next_metadata,
        }
        changed_fields = [
            field for field, changed in changes.items() if changed
        ]
        if changed_fields:
            previous_body_hash = hashlib.sha256(current["body"].encode()).hexdigest()
            next_body_hash = hashlib.sha256(next_body.encode()).hexdigest()
            previous_metadata_hash = _json_hash(dict(current["metadata"]))
            next_metadata_hash = _json_hash(next_metadata)
            cursor.execute(
                """
                UPDATE custom_contexts
                SET
                    context_type = %s,
                    body = %s,
                    applies_to = %s,
                    status = %s,
                    metadata = %s,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING updated_at
                """,
                (
                    next_type,
                    next_body,
                    list(next_applies_to),
                    next_status,
                    psycopg2.extras.Json(next_metadata),
                    context_id,
                ),
            )
            updated_at = cursor.fetchone()["updated_at"]
            cursor.execute(
                """
                INSERT INTO audit_events (
                    actor_kind,
                    actor_label,
                    action,
                    resource_kind,
                    resource_id,
                    repository_id,
                    details
                )
                VALUES (
                    %s,
                    %s,
                    'custom_context.updated',
                    'custom_context',
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    actor_kind,
                    normalized_actor,
                    str(context_id),
                    int(current["repository_id"]),
                    psycopg2.extras.Json(
                        {
                            "changed_fields": changed_fields,
                            "previous_updated_at": current[
                                "updated_at"
                            ].isoformat(),
                            "updated_at": updated_at.isoformat(),
                            "context_type": {
                                "from": current["context_type"],
                                "to": next_type,
                            },
                            "status": {
                                "from": current["status"],
                                "to": next_status,
                            },
                            "applies_to": {
                                "from": list(current["applies_to"]),
                                "to": list(next_applies_to),
                            },
                            "body_sha256": {
                                "from": previous_body_hash,
                                "to": next_body_hash,
                            },
                            "metadata_sha256": {
                                "from": previous_metadata_hash,
                                "to": next_metadata_hash,
                            },
                        }
                    ),
                ),
            )
        cursor.execute(
            """
            SELECT
                context.*,
                repository.full_name AS repository_full_name,
                repository.scm_provider,
                repository.scm_base_url
            FROM custom_contexts AS context
            JOIN repositories AS repository ON repository.id = context.repository_id
            WHERE context.id = %s
            """,
            (context_id,),
        )
        return {
            "customContext": custom_context_json(dict(cursor.fetchone())),
            "changed": bool(changed_fields),
        }


def delete_custom_context(
    conn,
    *,
    custom_context_id: str,
    expected_updated_at: str,
    actor_kind: Literal["operator", "service_token"],
    actor_label: str,
    actor_token_id: int | None,
    authorized_repository_ids: frozenset[int] | None,
) -> dict[str, object]:
    context_id = _resource_id(custom_context_id)
    expected_timestamp = _expected_timestamp(expected_updated_at)
    normalized_actor = _validate_actor(
        actor_kind=actor_kind,
        actor_label=actor_label,
        actor_token_id=actor_token_id,
    )
    authorization = "TRUE"
    authorization_parameters: list[object] = []
    if authorized_repository_ids is not None:
        authorization = "context.repository_id = ANY(%s)"
        authorization_parameters.append(sorted(authorized_repository_ids))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                context.*,
                repository.full_name AS repository_full_name,
                repository.scm_provider,
                repository.scm_base_url
            FROM custom_contexts AS context
            JOIN repositories AS repository ON repository.id = context.repository_id
            WHERE context.id = %s
              AND {authorization}
            FOR UPDATE OF context
            """,
            (context_id, *authorization_parameters),
        )
        current = cursor.fetchone()
        if current is None:
            raise ValueError("Custom context does not exist or is not authorized")
        if current["updated_at"] != expected_timestamp:
            raise ValueError("Custom context changed since expectedUpdatedAt")
        cursor.execute(
            """
            INSERT INTO audit_events (
                actor_kind,
                actor_label,
                action,
                resource_kind,
                resource_id,
                repository_id,
                details
            )
            VALUES (
                %s,
                %s,
                'custom_context.deleted',
                'custom_context',
                %s,
                %s,
                %s
            )
            RETURNING occurred_at
            """,
            (
                actor_kind,
                normalized_actor,
                str(context_id),
                int(current["repository_id"]),
                psycopg2.extras.Json(
                    {
                        "context_type": current["context_type"],
                        "status": current["status"],
                        "applies_to": list(current["applies_to"]),
                        "body_sha256": hashlib.sha256(
                            current["body"].encode()
                        ).hexdigest(),
                        "metadata_sha256": _json_hash(dict(current["metadata"])),
                        "created_at": current["created_at"].isoformat(),
                        "updated_at": current["updated_at"].isoformat(),
                    }
                ),
            ),
        )
        deleted_at = cursor.fetchone()["occurred_at"]
        cursor.execute(
            "DELETE FROM custom_contexts WHERE id = %s",
            (context_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Custom context deletion lost its row lock")
    return {
        "customContextId": f"custom_context_{context_id}",
        "deleted": True,
        "deletedAt": deleted_at.isoformat(),
        "repository": {
            "id": int(current["repository_id"]),
            "name": current["repository_full_name"],
            "remote": current["scm_provider"],
            "remoteUrl": current["scm_base_url"],
        },
    }


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
