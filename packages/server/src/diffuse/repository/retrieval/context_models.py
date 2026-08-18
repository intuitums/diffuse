"""Immutable snapshot plan for one primary and bounded related repositories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryContextSnapshot:
    repository_id: int
    repository_full_name: str
    snapshot_id: int
    commit_sha: str
    source: str
    cluster_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.repository_id <= 0
            or self.snapshot_id <= 0
            or len(self.commit_sha) < 7
            or self.source not in {"explicit", "cluster", "explicit+cluster"}
            or any(cluster_id <= 0 for cluster_id in self.cluster_ids)
        ):
            raise ValueError("Related-repository snapshot identity is invalid")


@dataclass(frozen=True)
class CrossRepositoryContextPlan:
    primary_repository_id: int
    primary_repository_full_name: str
    primary_snapshot_id: int | None
    primary_commit_sha: str | None
    related_snapshots: tuple[RepositoryContextSnapshot, ...] = ()
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            self.primary_repository_id <= 0
            or not self.primary_repository_full_name
            or (self.primary_snapshot_id is None) != (self.primary_commit_sha is None)
            or (
                self.primary_snapshot_id is not None
                and (
                    self.primary_snapshot_id <= 0
                    or self.primary_commit_sha is None
                    or len(self.primary_commit_sha) < 7
                )
            )
            or len(self.related_snapshots) > 7
        ):
            raise ValueError("Cross-repository context plan is invalid")
        repository_ids = [item.repository_id for item in self.related_snapshots]
        if (
            self.primary_repository_id in repository_ids
            or len(repository_ids) != len(set(repository_ids))
        ):
            raise ValueError("Cross-repository context plan contains duplicate repositories")
        expected = context_plan_fingerprint(
            primary_repository_id=self.primary_repository_id,
            primary_snapshot_id=self.primary_snapshot_id,
            related_snapshots=self.related_snapshots,
        )
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected)
        elif self.fingerprint != expected:
            raise ValueError("Cross-repository context fingerprint does not match its plan")


def context_plan_fingerprint(
    *,
    primary_repository_id: int,
    primary_snapshot_id: int | None,
    related_snapshots: tuple[RepositoryContextSnapshot, ...],
) -> str:
    payload = {
        "primary_repository_id": primary_repository_id,
        "primary_snapshot_id": primary_snapshot_id,
        "related": [
            {
                "repository_id": item.repository_id,
                "snapshot_id": item.snapshot_id,
                "commit_sha": item.commit_sha,
                "source": item.source,
                "cluster_ids": item.cluster_ids,
            }
            for item in related_snapshots
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
