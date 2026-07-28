"""Operator-managed repository clusters and exact cross-repository context plans."""

from __future__ import annotations

import re
from dataclasses import dataclass

import psycopg2.extras

from indexer.index_version import INDEX_FORMAT_VERSION
from retriever.context_models import (
    CrossRepositoryContextPlan,
    RepositoryContextSnapshot,
)

MAX_RELATED_REPOSITORIES = 7
CLUSTER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")

# Why an explicit `.diffuse` context repository can be refused. Lowercase reason
# codes, like trigger skip reasons and workflow error codes, so the value is
# stable enough for an operator to filter an audit trail on.
NOT_CLUSTER_MEMBER_REASON = "not_cluster_member"
CONTEXT_REPOSITORY_DROP_REASONS = frozenset({NOT_CLUSTER_MEMBER_REASON})


class CrossRepositoryContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class DroppedContextRepository:
    """One explicit context repository the resolver refused to expose."""

    repository_full_name: str
    reason_code: str

    def __post_init__(self) -> None:
        if (
            not 1 <= len(self.repository_full_name) <= 255
            or self.reason_code not in CONTEXT_REPOSITORY_DROP_REASONS
        ):
            raise ValueError("Dropped context repository identity is invalid")


@dataclass(frozen=True)
class CrossRepositoryContextResolution:
    """The plan that survived authorization, plus what it refused and why."""

    plan: CrossRepositoryContextPlan
    dropped_repositories: tuple[DroppedContextRepository, ...] = ()


@dataclass(frozen=True)
class RepositoryCluster:
    id: int
    scm_provider: str
    scm_base_url: str
    name: str
    description: str | None
    repository_ids: tuple[int, ...]
    repository_names: tuple[str, ...]


def _actor(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 255 or "\x00" in value:
        raise ValueError("Cluster operator identity is invalid")
    return value


def _cluster_name(value: str) -> str:
    value = value.strip()
    if not CLUSTER_NAME_PATTERN.fullmatch(value):
        raise ValueError("Cluster name must contain 1 to 100 safe characters")
    return value


def _description(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 500:
        raise ValueError("Cluster description must contain 1 to 500 characters")
    return value


def _load_repositories_for_cluster(conn, repository_ids: tuple[int, ...]):
    if (
        len(repository_ids) < 2
        or len(repository_ids) > MAX_RELATED_REPOSITORIES + 1
        or len(set(repository_ids)) != len(repository_ids)
        or any(repository_id <= 0 for repository_id in repository_ids)
    ):
        raise ValueError("A repository cluster must start with 2 to 8 unique repositories")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, scm_provider, scm_base_url, full_name
            FROM repositories
            WHERE id = ANY(%s)
              AND clone_url IS NOT NULL
            ORDER BY id
            FOR UPDATE
            """,
            (list(repository_ids),),
        )
        rows = cursor.fetchall()
    if len(rows) != len(repository_ids):
        raise ValueError("Every cluster repository must already be onboarded")
    hosts = {(row["scm_provider"], row["scm_base_url"]) for row in rows}
    if len(hosts) != 1:
        raise ValueError("All cluster repositories must use the same SCM provider and host")
    return rows


def _validate_related_limit(conn, repository_ids: tuple[int, ...]) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source.repository_id, count(DISTINCT target.repository_id)
            FROM repository_cluster_members AS source
            JOIN repository_cluster_members AS target
              ON target.cluster_id = source.cluster_id
             AND target.repository_id <> source.repository_id
            WHERE source.repository_id = ANY(%s)
            GROUP BY source.repository_id
            HAVING count(DISTINCT target.repository_id) > %s
            """,
            (list(repository_ids), MAX_RELATED_REPOSITORIES),
        )
        if cursor.fetchone():
            raise ValueError(
                f"Cluster membership may expose at most {MAX_RELATED_REPOSITORIES} "
                "related repositories per source repository"
            )


def create_repository_cluster(
    conn,
    *,
    name: str,
    repository_ids: tuple[int, ...],
    actor_login: str,
    description: str | None = None,
) -> RepositoryCluster:
    name = _cluster_name(name)
    actor_login = _actor(actor_login)
    description = _description(description)
    rows = _load_repositories_for_cluster(conn, repository_ids)
    provider = rows[0]["scm_provider"]
    base_url = rows[0]["scm_base_url"]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            INSERT INTO repository_clusters (
                scm_provider,
                scm_base_url,
                name,
                description,
                created_by
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (provider, base_url, name, description, actor_login),
        )
        cluster_id = int(cursor.fetchone()["id"])
        psycopg2.extras.execute_values(
            cursor,
            """
            INSERT INTO repository_cluster_members (
                cluster_id,
                repository_id,
                added_by
            )
            VALUES %s
            """,
            [
                (cluster_id, int(row["id"]), actor_login)
                for row in rows
            ],
        )
    _validate_related_limit(
        conn,
        tuple(int(row["id"]) for row in rows),
    )
    return RepositoryCluster(
        id=cluster_id,
        scm_provider=provider,
        scm_base_url=base_url,
        name=name,
        description=description,
        repository_ids=tuple(int(row["id"]) for row in rows),
        repository_names=tuple(row["full_name"] for row in rows),
    )


def add_repository_cluster_member(
    conn,
    *,
    cluster_id: int,
    repository_id: int,
    actor_login: str,
) -> bool:
    actor_login = _actor(actor_login)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT scm_provider, scm_base_url
            FROM repository_clusters
            WHERE id = %s
            FOR UPDATE
            """,
            (cluster_id,),
        )
        cluster = cursor.fetchone()
        cursor.execute(
            """
            SELECT scm_provider, scm_base_url, clone_url
            FROM repositories
            WHERE id = %s
            FOR UPDATE
            """,
            (repository_id,),
        )
        repository = cursor.fetchone()
        if not cluster or not repository:
            raise ValueError("Cluster or repository does not exist")
        if not repository["clone_url"]:
            raise ValueError("Cluster repository must already be onboarded")
        if (
            cluster["scm_provider"] != repository["scm_provider"]
            or cluster["scm_base_url"] != repository["scm_base_url"]
        ):
            raise ValueError("Cluster members must use the cluster's SCM provider and host")
        cursor.execute(
            """
            INSERT INTO repository_cluster_members (
                cluster_id,
                repository_id,
                added_by
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (cluster_id, repository_id) DO NOTHING
            """,
            (cluster_id, repository_id, actor_login),
        )
        added = cursor.rowcount == 1
        cursor.execute(
            """
            SELECT DISTINCT repository_id
            FROM repository_cluster_members
            WHERE cluster_id IN (
                SELECT cluster_id
                FROM repository_cluster_members
                WHERE repository_id = %s
            )
            """,
            (repository_id,),
        )
        affected = tuple(int(row["repository_id"]) for row in cursor.fetchall())
    _validate_related_limit(conn, affected)
    return added


def remove_repository_cluster_member(
    conn,
    *,
    cluster_id: int,
    repository_id: int,
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM repository_clusters WHERE id = %s FOR UPDATE",
            (cluster_id,),
        )
        if not cursor.fetchone():
            return False
        cursor.execute(
            """
            SELECT 1
            FROM repository_cluster_members
            WHERE cluster_id = %s
              AND repository_id = %s
            """,
            (cluster_id, repository_id),
        )
        if not cursor.fetchone():
            return False
        cursor.execute(
            """
            SELECT count(*)
            FROM repository_cluster_members
            WHERE cluster_id = %s
            """,
            (cluster_id,),
        )
        count = int(cursor.fetchone()[0])
        if count <= 2:
            raise ValueError(
                "A repository cluster must keep at least two members; delete it instead"
            )
        cursor.execute(
            """
            DELETE FROM repository_cluster_members
            WHERE cluster_id = %s
              AND repository_id = %s
            """,
            (cluster_id, repository_id),
        )
        return cursor.rowcount == 1


def delete_repository_cluster(conn, *, cluster_id: int) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM repository_clusters WHERE id = %s",
            (cluster_id,),
        )
        return cursor.rowcount == 1


def list_repository_clusters(conn) -> tuple[RepositoryCluster, ...]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                cluster.id,
                cluster.scm_provider,
                cluster.scm_base_url,
                cluster.name,
                cluster.description,
                COALESCE(
                    array_agg(repository.id ORDER BY repository.id)
                        FILTER (WHERE repository.id IS NOT NULL),
                    ARRAY[]::BIGINT[]
                ) AS repository_ids,
                COALESCE(
                    array_agg(repository.full_name ORDER BY repository.id)
                        FILTER (WHERE repository.id IS NOT NULL),
                    ARRAY[]::TEXT[]
                ) AS repository_names
            FROM repository_clusters AS cluster
            LEFT JOIN repository_cluster_members AS member
              ON member.cluster_id = cluster.id
            LEFT JOIN repositories AS repository ON repository.id = member.repository_id
            GROUP BY cluster.id
            ORDER BY cluster.scm_provider, cluster.scm_base_url, cluster.name
            """
        )
        rows = cursor.fetchall()
    return tuple(
        RepositoryCluster(
            id=int(row["id"]),
            scm_provider=row["scm_provider"],
            scm_base_url=row["scm_base_url"],
            name=row["name"],
            description=row["description"],
            repository_ids=tuple(int(value) for value in row["repository_ids"]),
            repository_names=tuple(row["repository_names"]),
        )
        for row in rows
    )


def _active_snapshot(
    cursor,
    *,
    repository_id: int,
    model: str,
    dimensions: int,
):
    cursor.execute(
        """
        SELECT id, commit_sha
        FROM index_snapshots
        WHERE repository_id = %s
          AND status = 'active'
          AND index_format_version = %s
          AND embedding_model = %s
          AND embedding_dimensions = %s
        """,
        (repository_id, INDEX_FORMAT_VERSION, model, dimensions),
    )
    return cursor.fetchone()


def resolve_cross_repository_context_plan(
    conn,
    *,
    primary_repository_id: int,
    primary_snapshot_id: int | None,
    explicit_repositories: tuple[str, ...],
    model: str,
    dimensions: int,
) -> CrossRepositoryContextPlan:
    """Resolve the plan alone, for callers that cannot act on a refusal."""
    return resolve_cross_repository_context(
        conn,
        primary_repository_id=primary_repository_id,
        primary_snapshot_id=primary_snapshot_id,
        explicit_repositories=explicit_repositories,
        model=model,
        dimensions=dimensions,
    ).plan


def resolve_cross_repository_context(
    conn,
    *,
    primary_repository_id: int,
    primary_snapshot_id: int | None,
    explicit_repositories: tuple[str, ...],
    model: str,
    dimensions: int,
) -> CrossRepositoryContextResolution:
    """Plan cross-repository retrieval under the operator authorization boundary.

    ``explicit_repositories`` comes from ``context.repos`` in a ``.diffuse``
    file that is committed to the repository under review, so it is attacker
    controlled by anyone with merge access there. An entry is therefore a
    *request* to narrow retrieval, never a grant: it is honoured only when an
    operator has already placed that repository in a cluster with the primary
    one, which is the same boundary the REST and MCP surfaces enforce. Entries
    the operator never clustered are dropped and reported, not fatal -- a
    misconfigured ``.diffuse`` must not stop the review it belongs to.
    """
    if len(explicit_repositories) > MAX_RELATED_REPOSITORIES:
        raise CrossRepositoryContextError(
            f"At most {MAX_RELATED_REPOSITORIES} explicit context repositories are allowed"
        )
    explicit_identities = tuple(value.casefold() for value in explicit_repositories)
    if len(set(explicit_identities)) != len(explicit_identities):
        raise CrossRepositoryContextError("Explicit context repositories must be unique")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, scm_provider, scm_base_url, full_name, enabled
            FROM repositories
            WHERE id = %s
            """,
            (primary_repository_id,),
        )
        primary = cursor.fetchone()
        if not primary or not primary["enabled"]:
            raise CrossRepositoryContextError("Primary repository is not enabled")
        primary_commit_sha = None
        if primary_snapshot_id is not None:
            cursor.execute(
                """
                SELECT commit_sha
                FROM index_snapshots
                WHERE id = %s
                  AND repository_id = %s
                  AND index_format_version = %s
                  AND embedding_model = %s
                  AND embedding_dimensions = %s
                """,
                (
                    primary_snapshot_id,
                    primary_repository_id,
                    INDEX_FORMAT_VERSION,
                    model,
                    dimensions,
                ),
            )
            snapshot = cursor.fetchone()
            if not snapshot:
                raise CrossRepositoryContextError(
                    "Primary snapshot is not compatible with cross-repository retrieval"
                )
            primary_commit_sha = snapshot["commit_sha"]

        cursor.execute(
            """
            SELECT
                repository.id,
                repository.full_name,
                repository.enabled,
                repository.scm_provider,
                repository.scm_base_url,
                array_agg(DISTINCT cluster.id ORDER BY cluster.id) AS cluster_ids
            FROM repository_cluster_members AS source
            JOIN repository_cluster_members AS related
              ON related.cluster_id = source.cluster_id
             AND related.repository_id <> source.repository_id
            JOIN repository_clusters AS cluster ON cluster.id = source.cluster_id
            JOIN repositories AS repository ON repository.id = related.repository_id
            WHERE source.repository_id = %s
            GROUP BY repository.id
            ORDER BY repository.full_name
            """,
            (primary_repository_id,),
        )
        cluster_targets = {}
        for row in cursor.fetchall():
            if (
                row["scm_provider"] != primary["scm_provider"]
                or row["scm_base_url"] != primary["scm_base_url"]
            ):
                raise CrossRepositoryContextError(
                    "Repository cluster contains a member from another SCM host"
                )
            if row["enabled"]:
                cluster_targets[row["full_name"].casefold()] = row

        selected: list[RepositoryContextSnapshot] = []
        selected_names: set[str] = set()
        dropped: list[DroppedContextRepository] = []
        for repository_name in explicit_repositories:
            repository_identity = repository_name.casefold()
            if repository_identity == primary["full_name"].casefold():
                raise CrossRepositoryContextError(
                    "A repository cannot list itself as cross-repository context"
                )
            cursor.execute(
                """
                SELECT id, full_name, enabled
                FROM repositories
                WHERE scm_provider = %s
                  AND scm_base_url = %s
                  AND full_name = %s
                  AND clone_url IS NOT NULL
                """,
                (
                    primary["scm_provider"],
                    primary["scm_base_url"],
                    repository_name,
                ),
            )
            target = cursor.fetchone()
            if not target or not target["enabled"]:
                raise CrossRepositoryContextError(
                    f"Explicit context repository is not enabled on the same SCM host: "
                    f"{repository_name}"
                )
            cluster = cluster_targets.get(repository_identity)
            if cluster is None:
                # Naming an onboarded repository is not authorization to read
                # its indexed source. Without this, merge access to any low
                # value repository would pull a high-value repository's chunks
                # into the review context.
                dropped.append(
                    DroppedContextRepository(
                        repository_full_name=target["full_name"],
                        reason_code=NOT_CLUSTER_MEMBER_REASON,
                    )
                )
                continue
            active = _active_snapshot(
                cursor,
                repository_id=int(target["id"]),
                model=model,
                dimensions=dimensions,
            )
            if not active:
                raise CrossRepositoryContextError(
                    f"Explicit context repository has no compatible active snapshot: "
                    f"{repository_name}"
                )
            selected.append(
                RepositoryContextSnapshot(
                    repository_id=int(target["id"]),
                    repository_full_name=target["full_name"],
                    snapshot_id=int(active["id"]),
                    commit_sha=active["commit_sha"],
                    source="explicit+cluster",
                    cluster_ids=tuple(int(value) for value in cluster["cluster_ids"]),
                )
            )
            selected_names.add(repository_identity)

        for repository_identity, target in cluster_targets.items():
            if repository_identity in selected_names:
                continue
            active = _active_snapshot(
                cursor,
                repository_id=int(target["id"]),
                model=model,
                dimensions=dimensions,
            )
            if not active:
                continue
            selected.append(
                RepositoryContextSnapshot(
                    repository_id=int(target["id"]),
                    repository_full_name=target["full_name"],
                    snapshot_id=int(active["id"]),
                    commit_sha=active["commit_sha"],
                    source="cluster",
                    cluster_ids=tuple(int(value) for value in target["cluster_ids"]),
                )
            )
            selected_names.add(repository_identity)

    if len(selected) > MAX_RELATED_REPOSITORIES:
        raise CrossRepositoryContextError(
            f"Explicit configuration and clusters expose more than "
            f"{MAX_RELATED_REPOSITORIES} related repositories"
        )
    return CrossRepositoryContextResolution(
        plan=CrossRepositoryContextPlan(
            primary_repository_id=primary_repository_id,
            primary_repository_full_name=primary["full_name"],
            primary_snapshot_id=primary_snapshot_id,
            primary_commit_sha=primary_commit_sha,
            related_snapshots=tuple(selected),
        ),
        dropped_repositories=tuple(dropped),
    )


def record_dropped_context_repositories(
    conn,
    *,
    primary_repository_id: int,
    actor_label: str,
    dropped: tuple[DroppedContextRepository, ...],
) -> None:
    """Record every explicit context repository the resolver refused.

    Dropping is deliberately not fatal, so without a durable row the refusal
    would survive only as a log line on whichever worker happened to run the
    review -- and a ``.diffuse`` reaching for a repository it was never granted
    is exactly the thing an operator needs to be able to find later.
    ``audit_events`` is where this repository already keeps operator-visible
    decisions, so the refusal lands beside the cluster changes it is about.
    """
    if not dropped:
        return
    actor_label = _actor(actor_label)
    with conn.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
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
            VALUES %s
            """,
            [
                (
                    "system",
                    actor_label,
                    "review.context_repository_dropped",
                    "repository",
                    item.repository_full_name,
                    primary_repository_id,
                    psycopg2.extras.Json({"reason": item.reason_code}),
                )
                for item in dropped
            ],
        )
