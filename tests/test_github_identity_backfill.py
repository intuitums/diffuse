"""Worker-owned backfill of immutable GitHub repository identities."""

from __future__ import annotations

from types import SimpleNamespace

from service.github.repository import GitHubRepositoryMetadata
from service.hosted import worker
from service.repositories import RepositoryIdentityConflictError


class _NullConn:
    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _repository(name: str):
    return SimpleNamespace(scm_base_url="https://github.com", full_name=name)


def test_worker_backfill_binds_legacy_repositories_without_a_cli_command(monkeypatch):
    legacy = _repository("old-owner/old-name")
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(
        worker,
        "list_unbound_github_repositories",
        lambda _conn, *, limit: [legacy] if limit == 25 else [],
    )
    monkeypatch.setattr(
        worker,
        "fetch_github_repository_metadata",
        lambda _repository: GitHubRepositoryMetadata(
            repository_id=123,
            full_name="new-owner/new-name",
            default_branch="main",
        ),
    )
    monkeypatch.setattr(
        worker,
        "resolve_github_repository",
        lambda _conn, **kwargs: captured.update(kwargs) or object(),
    )

    assert worker._backfill_github_repository_identities() == 1
    assert captured == {
        "scm_base_url": "https://github.com",
        "github_repository_id": 123,
        "full_name": "new-owner/new-name",
        "default_branch": "main",
        "actor_label": "github-identity-backfill",
    }


def test_worker_backfill_continues_after_an_identity_collision(monkeypatch):
    conflicting = _repository("acme/conflicting")
    healthy = _repository("acme/healthy")
    calls: list[str] = []

    monkeypatch.setattr(worker, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(
        worker,
        "list_unbound_github_repositories",
        lambda _conn, *, limit: [conflicting, healthy],
    )
    monkeypatch.setattr(
        worker,
        "fetch_github_repository_metadata",
        lambda repository: GitHubRepositoryMetadata(
            repository_id=1 if repository is conflicting else 2,
            full_name=repository.full_name,
            default_branch="main",
        ),
    )

    def resolve(_conn, **kwargs):
        calls.append(str(kwargs["full_name"]))
        if kwargs["full_name"] == conflicting.full_name:
            raise RepositoryIdentityConflictError("collision")
        return object()

    monkeypatch.setattr(worker, "resolve_github_repository", resolve)

    assert worker._backfill_github_repository_identities() == 1
    assert calls == ["acme/conflicting", "acme/healthy"]
