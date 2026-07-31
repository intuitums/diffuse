"""Database access for invites."""

from __future__ import annotations


class Connection:
    """A pooled handle in PostgreSQL's default READ COMMITTED isolation.

    Every statement runs in its own implicitly committed transaction unless a
    caller opens one explicitly, and nothing here takes a row lock. Requests
    are served concurrently by several worker processes.
    """

    def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        raise NotImplementedError

    def execute(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError
