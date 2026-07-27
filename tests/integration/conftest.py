"""Shared configuration for PostgreSQL integration tests.

Every test under this directory is an integration test and every one of them
needs `POSTGRES_TEST_DATABASE_URL`. Marking that once here means a new test file
cannot forget it. The previous arrangement repeated a byte-identical
`pytestmark` block in all ten modules, and a file that omitted it would have run
against whatever `DATABASE_URL` happened to be set -- which, for a developer with
a local stack running, is their real database.
"""

from __future__ import annotations

import os
import pathlib

import pytest


def pytest_configure() -> None:
    """Point application connections at the disposable integration database."""
    database_url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if database_url:
        os.environ["DATABASE_URL"] = database_url


_HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items) -> None:
    """Mark everything *here* integration, and skip it when no database is configured.

    The hook receives every collected item in the session, not only the ones
    under this directory, so it must filter by path. Marking indiscriminately
    would tag the whole unit suite as integration -- and `-m "not integration"`
    would then deselect all of it and report success having run nothing.
    """
    configured = bool(os.environ.get("POSTGRES_TEST_DATABASE_URL"))
    skip = pytest.mark.skip(reason="POSTGRES_TEST_DATABASE_URL is not configured")
    for item in items:
        if _HERE not in pathlib.Path(str(item.fspath)).parents:
            continue
        item.add_marker(pytest.mark.integration)
        if not configured:
            item.add_marker(skip)
