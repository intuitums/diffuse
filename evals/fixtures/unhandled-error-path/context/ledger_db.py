"""Pooled database handles for the ledger."""

from __future__ import annotations


class ConstraintViolation(Exception):
    """Raised when a row violates a database constraint."""


class Connection:
    """A connection leased from a fixed-size pool.

    `close()` returns the handle to the pool. A connection that is not closed
    is never reissued, so a leak on an error path exhausts the pool and the
    process stops accepting work until it is restarted.
    """

    def begin(self) -> None:
        raise NotImplementedError

    def insert(self, table: str, row: dict) -> None:
        """Insert one row. Raises ConstraintViolation, or any driver error."""
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
