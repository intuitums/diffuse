"""Scheduled reporting jobs."""

from __future__ import annotations

from reports.db import connect
from reports.rollup import month_key, rollup_usage
from reports.store import all_active_account_ids


def nightly_rollup(year: int, month: int) -> dict:
    """Roll up metered usage for every active account.

    `all_active_account_ids()` returns the whole tenant base -- on the order of
    tens of thousands of ids -- and this job runs inside a 15-minute scheduler
    window.
    """
    account_ids = all_active_account_ids()
    with connect() as connection:
        return rollup_usage(connection, account_ids, month_key(year, month))
