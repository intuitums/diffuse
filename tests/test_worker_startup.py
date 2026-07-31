"""Startup configuration validation.

`validate_worker_configuration()` is the probe list that resolves every hot-path
variable before any job is claimed, and it is live configuration validation that
merely happens to be hosted in `service/worker.py` today -- W5.1 makes it the
shared implementation for every entry point. Its tests went out with
`tests/test_worker.py`, leaving the whole probe list with nothing holding it, so
they are recovered here in a file the rebuild keeps.

`tests/test_review_depth.py` survived the same deletion only because its
filename was not on the doomed list; putting these there would compound that
accident.

One further test travelled with these -- an assertion that an *unset*
`REVIEW_MODEL` refuses startup by name. It pins behaviour this base does not
have (`review_engine.DEFAULT_REVIEW_MODEL` still exists here), so it belongs
with the change that deletes the default rather than in this file.
"""

import pytest

from service import worker


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REVIEW_PASSES", "correctness,perf"),
        ("MIN_REVIEW_CONFIDENCE", "1.5"),
        ("REVIEW_MODEL", "   "),
        ("REVIEW_PROVENANCE_MIN_CONFIDENCE", "high"),
        ("REVIEW_STRUCTURED_OUTPUT_MODE", "strict"),
        ("REVIEW_MAX_OUTPUT_TOKENS", "0"),
        ("REVIEW_MODEL_TIMEOUT_SECONDS", "none"),
        ("REVIEW_DIFF_CHARS_PER_CALL", "-1"),
        ("REVIEW_MAX_DIFF_CHUNKS", "many"),
        ("DIFFUSE_MAX_REPOSITORY_BYTES", "0"),
        ("MAX_CONTEXT_CHUNKS", "0"),
        ("MIN_CONTEXT_SIMILARITY", "2"),
        ("WORKFLOW_LEASE_SECONDS", "30"),
        ("EMBEDDING_DIMENSIONS", "wide"),
        ("SCM_API_TIMEOUT_SECONDS", "0"),
    ],
)
def test_every_hot_path_variable_fails_startup_by_name(monkeypatch, name, value):
    # REVIEW_MODEL is probed early, so every case below pins its own variable
    # only while REVIEW_MODEL itself resolves. Setting it explicitly keeps that
    # true whether or not a default happens to exist.
    if name != "REVIEW_MODEL":
        monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        worker.validate_worker_configuration()


def test_startup_configuration_check_accepts_the_shipped_defaults(monkeypatch):
    for name, _probe in worker._CONFIGURATION_PROBES:
        monkeypatch.delenv(name, raising=False)
    # Named explicitly rather than left to a default: what this test is for is
    # the *other* probes accepting their shipped values, and REVIEW_MODEL is the
    # variable whose default is under active change.
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")

    worker.validate_worker_configuration()
