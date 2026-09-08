"""Shared configuration for PostgreSQL integration tests.

Every test under this directory is an integration test and every one of them
needs `POSTGRES_TEST_DATABASE_URL`. Marking that once here means a new test file
cannot forget it. The previous arrangement repeated a byte-identical
`pytestmark` block in every module, and a file that omitted it would have run
against whatever `DATABASE_URL` happened to be set -- which, for a developer with
a local stack running, is their real database.
"""

from __future__ import annotations

import os
import pathlib

import pytest

# Skipping is the right default for a laptop with no PostgreSQL, and the wrong one
# for CI: `pytest -m integration` with the variable unset exits 0 having asserted
# nothing, so a renamed variable or a service container that never bound its port
# reports success. Anywhere the suite is *expected* to run, set this and a missing
# database becomes an error instead of a green skip.
REQUIRE_VARIABLE = "DIFFUSE_REQUIRE_INTEGRATION_TESTS"
DATABASE_VARIABLE = "POSTGRES_TEST_DATABASE_URL"

_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _integration_tests_are_required() -> bool:
    return os.environ.get(REQUIRE_VARIABLE, "").strip().lower() not in _FALSEY


def pytest_configure() -> None:
    """Point application connections at the disposable integration database."""
    database_url = os.environ.get(DATABASE_VARIABLE)
    if database_url:
        os.environ["DATABASE_URL"] = database_url
        return
    if _integration_tests_are_required():
        raise pytest.UsageError(
            f"{REQUIRE_VARIABLE} is set, so the PostgreSQL integration suite must "
            f"actually run, but {DATABASE_VARIABLE} is unset or empty. Either point "
            "it at a disposable database or unset the requirement."
        )


@pytest.fixture(autouse=True)
def no_update_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claim a pushed revision immediately unless the test is about the wait.

    Most store tests reach `enqueue_review_event` only for the `workflow_jobs`
    row their schema requires, and several of those fixtures use a `synchronize`
    action because lineage is what they are testing. With the shipped
    `REVIEW_UPDATE_DEBOUNCE_SECONDS` those jobs are not claimable for a minute
    and `claim_workflow_job` returns `None`, which reads as a queue bug rather
    than as the debounce doing its job.

    A test that *is* about the wait sets the variable itself; `monkeypatch.setenv`
    in the test body wins over the value this fixture set first.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "0")


_HERE = pathlib.Path(__file__).parent

# Node ids of everything collected from this directory, and of everything from it
# that reached its call phase. A skipped test never reaches `call`, so comparing
# the two is what distinguishes "the suite ran" from "the suite was skipped".
_collected: set[str] = set()
_executed: set[str] = set()


def pytest_collection_modifyitems(items) -> None:
    """Mark everything *here* integration, and skip it when no database is configured.

    The hook receives every collected item in the session, not only the ones
    under this directory, so it must filter by path. Marking indiscriminately
    would tag the whole unit suite as integration -- and `-m "not integration"`
    would then deselect all of it and report success having run nothing.
    """
    configured = bool(os.environ.get(DATABASE_VARIABLE))
    skip = pytest.mark.skip(reason=f"{DATABASE_VARIABLE} is not configured")
    for item in items:
        if _HERE not in pathlib.Path(str(item.fspath)).parents:
            continue
        _collected.add(item.nodeid)
        item.add_marker(pytest.mark.integration)
        if not configured:
            item.add_marker(skip)


def pytest_runtest_logreport(report) -> None:
    """Record which integration tests got as far as executing their body."""
    if report.when == "call" and report.nodeid in _collected:
        _executed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Refuse to exit 0 having run no integration test when one was required.

    `pytest_configure` catches the unset-variable case. This catches every other
    way the count reaches zero -- a `-m` expression that stopped matching, a
    directory rename that emptied collection, a conftest-level skip added later --
    which is the failure mode that makes this job's green meaningless.
    """
    if not _integration_tests_are_required():
        return
    if exitstatus != 0 or _executed:
        return
    session.exitstatus = 1
    print(
        f"\nERROR: {REQUIRE_VARIABLE} is set but 0 of {len(_collected)} collected "
        "integration tests executed. A pass here would assert nothing."
    )
