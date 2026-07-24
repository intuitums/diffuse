from datetime import UTC, datetime

import pytest

from service.analytics_store import (
    MAX_ANALYTICS_WINDOW_DAYS,
    _author,
    _rate,
    _timestamp,
    _window,
)


def test_analytics_timestamps_require_an_explicit_timezone():
    with pytest.raises(ValueError, match="timezone"):
        _timestamp("2026-07-01T00:00:00", field="startAt")
    with pytest.raises(ValueError, match="ISO-8601"):
        _timestamp("not-a-date", field="startAt")

    assert _timestamp("2026-07-01T00:00:00Z", field="startAt") == datetime(
        2026,
        7,
        1,
        tzinfo=UTC,
    )


def test_analytics_window_is_half_open_bounded_and_normalized_to_utc():
    start, end = _window(
        "2026-07-01T01:00:00+01:00",
        "2026-07-02T02:00:00+02:00",
    )

    assert start.isoformat() == "2026-07-01T00:00:00+00:00"
    assert end.isoformat() == "2026-07-02T00:00:00+00:00"

    with pytest.raises(ValueError, match="later"):
        _window("2026-07-02T00:00:00Z", "2026-07-01T00:00:00Z")
    with pytest.raises(ValueError, match=str(MAX_ANALYTICS_WINDOW_DAYS)):
        _window("2025-01-01T00:00:00Z", "2026-07-01T00:00:00Z")


def test_analytics_rates_make_empty_denominators_explicit():
    assert _rate(0, 0) is None
    assert _rate(1, 3) == 33.33
    assert _rate(2, 2) == 100.0


def test_analytics_author_filter_is_exact_and_bounded():
    assert _author(None) is None
    assert _author("  octocat  ") == "octocat"

    for value in ("", " \t ", "bad\x00actor", "x" * 256):
        with pytest.raises(ValueError, match="author"):
            _author(value)
