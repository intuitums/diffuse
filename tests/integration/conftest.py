"""Shared configuration for PostgreSQL integration tests."""

from __future__ import annotations

import os


def pytest_configure() -> None:
    """Point application connections at the disposable integration database."""
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if database_url:
        os.environ["DATABASE_URL"] = database_url
