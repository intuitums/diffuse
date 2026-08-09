"""`maintenance reindex --all` is the post-upgrade reindex sweep.

A release that changes `INDEX_FORMAT_VERSION` invalidates every existing index
snapshot. Retrieval refuses an incompatible snapshot and `process_review_job`
raises `MissingRepositoryIndexError`, so without a way to reindex everything an
upgrade takes every repository's reviews offline until each is touched by hand.
"""

from __future__ import annotations

import argparse

import pytest

from service.cli import maintenance as maintenance_cli
from service.cli import repository as repository_cli


class _Repository:
    def __init__(self, identifier: int, full_name: str, *, enabled: bool = True):
        self.id = identifier
        self.full_name = full_name
        self.enabled = enabled


def _args(repository: str | None = None, *, all_: bool = False):
    return argparse.Namespace(repository=repository, base_url=None, all=all_)


def test_reindex_all_queues_every_enabled_repository(monkeypatch, capsys):
    repositories = [
        _Repository(1, "acme/api"),
        _Repository(2, "acme/web"),
        _Repository(3, "acme/retired", enabled=False),
    ]
    queued: list[int] = []

    monkeypatch.setattr(repository_cli, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(repository_cli, "list_repositories", lambda _conn: repositories)
    monkeypatch.setattr(
        repository_cli,
        "enqueue_initial_index",
        lambda repository: (queued.append(repository.id) or (100 + repository.id, "a" * 40)),
    )

    repository_cli._reindex_repository(_args(all_=True))

    # The disabled repository is skipped; enabling it queues it.
    assert queued == [1, 2]
    out = capsys.readouterr().out
    assert "Queued 2 of 2 enabled repositories." in out


def test_reindex_all_continues_past_one_unreachable_mirror(monkeypatch, capsys):
    repositories = [_Repository(1, "acme/api"), _Repository(2, "acme/web")]

    def enqueue(repository):
        if repository.id == 1:
            raise RuntimeError("mirror unreachable")
        return (200, "b" * 40)

    monkeypatch.setattr(repository_cli, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(repository_cli, "list_repositories", lambda _conn: repositories)
    monkeypatch.setattr(repository_cli, "enqueue_initial_index", enqueue)

    # A partial sweep must be loud, but must still queue the reachable ones.
    with pytest.raises(RuntimeError, match="1 of 2 repositories could not be queued"):
        repository_cli._reindex_repository(_args(all_=True))

    out = capsys.readouterr().out
    assert "acme/api: could not queue: mirror unreachable" in out
    assert "acme/web: queued index job 200" in out


def test_reindex_rejects_ambiguous_and_empty_invocations(monkeypatch):
    monkeypatch.setattr(repository_cli, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(repository_cli, "list_repositories", lambda _conn: [])

    with pytest.raises(ValueError, match="not both"):
        repository_cli._reindex_repository(_args("acme/api", all_=True))
    with pytest.raises(ValueError, match="--all to reindex"):
        repository_cli._reindex_repository(_args(None))


def test_reindex_all_reports_an_empty_fleet_without_failing(monkeypatch, capsys):
    monkeypatch.setattr(repository_cli, "get_conn", lambda: _NullConn())
    monkeypatch.setattr(repository_cli, "list_repositories", lambda _conn: [])

    repository_cli._reindex_repository(_args(all_=True))

    assert "No enabled repositories to reindex." in capsys.readouterr().out


def test_maintenance_reindex_parser_accepts_all_and_a_repository_name():
    parser = argparse.ArgumentParser()
    maintenance_cli.configure_parser(parser)

    assert parser.parse_args(["reindex", "--all"]).all is True
    assert parser.parse_args(["reindex", "acme/api"]).repository == "acme/api"
    assert parser.parse_args(["reindex", "acme/api"]).all is False


class _NullConn:
    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False
