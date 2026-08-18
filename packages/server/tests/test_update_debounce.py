"""How `REVIEW_UPDATE_DEBOUNCE_SECONDS` is read.

The behaviour it produces -- a burst of pushes collapsing into one review of the
final head -- is asserted in `tests/integration/test_update_debounce_postgres.py`,
because the deadline arithmetic happens in SQL. What is testable in process is
the parse, and that matters on its own: the value is read inside a webhook
handler, where a malformed one would turn every push into a 500 rather than a
startup failure naming the variable. `tests/test_worker_startup.py` covers the
startup refusal; these cover what it refuses.
"""

import pytest
from diffuse.review.workflow import (
    DEFAULT_UPDATE_DEBOUNCE_SECONDS,
    review_update_debounce_seconds,
)


def test_the_shipped_default_waits_out_a_push_burst(monkeypatch):
    monkeypatch.delenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", raising=False)

    assert review_update_debounce_seconds() == DEFAULT_UPDATE_DEBOUNCE_SECONDS
    assert DEFAULT_UPDATE_DEBOUNCE_SECONDS > 0


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_value_is_the_default_rather_than_zero(monkeypatch, value):
    """`FOO=` in an env file means "unset", not "disable the wait".

    Compose passes an empty assignment through as an empty string, so reading it
    as `0` would silently turn the debounce off for anyone who left the line in
    place without a value -- the opposite of what an empty line looks like it
    does.
    """
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", value)

    assert review_update_debounce_seconds() == DEFAULT_UPDATE_DEBOUNCE_SECONDS


def test_zero_is_honoured_as_the_documented_opt_out(monkeypatch):
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", "0")

    assert review_update_debounce_seconds() == 0


@pytest.mark.parametrize("value", ["-1", "60.5", "a minute"])
def test_a_value_that_is_not_whole_seconds_is_refused(monkeypatch, value):
    monkeypatch.setenv("REVIEW_UPDATE_DEBOUNCE_SECONDS", value)

    with pytest.raises(ValueError, match="REVIEW_UPDATE_DEBOUNCE_SECONDS"):
        review_update_debounce_seconds()
