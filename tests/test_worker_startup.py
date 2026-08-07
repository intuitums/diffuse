"""Startup configuration validation.

`validate_worker_configuration()` is the probe list that resolves every hot-path
variable before any job is claimed, and it is live configuration validation that
merely happens to be hosted in `service/hosted/worker.py` today -- W5.1 makes it the
shared implementation for every entry point. Its tests went out with
`tests/test_worker.py`, leaving `REVIEW_MODEL`'s no-default refusal (PR #45's
headline behaviour) with nothing holding it, so they are recovered here in a
file the rebuild keeps.

`tests/test_review_depth.py` survived the same deletion only because its
filename was not on the doomed list; putting these there would compound that
accident.
"""

import pytest

from service.hosted import worker


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
        ("WORKFLOW_LEASE_SECONDS", "30"),
        ("SCM_API_TIMEOUT_SECONDS", "0"),
        ("REVIEW_UPDATE_DEBOUNCE_SECONDS", "-1"),
        ("REVIEW_UPDATE_DEBOUNCE_SECONDS", "a minute"),
    ],
)
def test_every_hot_path_variable_fails_startup_by_name(monkeypatch, name, value):
    # REVIEW_MODEL has no default and is probed early, so without a valid value
    # every case below would report REVIEW_MODEL instead of the variable it is
    # meant to exercise.
    if name != "REVIEW_MODEL":
        monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        worker.validate_worker_configuration()


def test_startup_configuration_check_accepts_the_shipped_defaults(monkeypatch):
    for name, _probe in worker._CONFIGURATION_PROBES:
        monkeypatch.delenv(name, raising=False)
    # REVIEW_MODEL is the one hot-path variable with no default, by design: see
    # `test_unset_review_model_stops_the_worker_with_an_actionable_error`.
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")

    worker.validate_worker_configuration()


def test_unset_review_model_stops_the_worker_with_an_actionable_error(monkeypatch):
    """No default model, and the refusal must tell the operator what to do.

    Diffuse used to fall back to a hardcoded `anthropic/claude-sonnet-5`, which
    assumes a credential the operator may never have had. Guessing is the bug,
    so the only acceptable behaviour is a startup refusal that names the
    variable and points at the setup path.
    """
    for name, _probe in worker._CONFIGURATION_PROBES:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError) as failure:
        worker.validate_worker_configuration()

    message = str(failure.value)
    assert "REVIEW_MODEL" in message
    assert "diffuse init" in message
    # A refusal that merely says "unset" leaves the operator guessing at the
    # format; it has to show one.
    assert "anthropic/claude-sonnet-5" in message


def test_native_runtime_does_not_require_litellm_review_model_configuration(monkeypatch):
    """CLI-native review mode has credentials only in its isolated runners."""

    for name, _probe in (
        *worker._CONFIGURATION_PROBES,
        *worker._LITELLM_CONFIGURATION_PROBES,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REVIEW_RUNTIME", "codex")
    monkeypatch.setenv("DIFFUSE_AGENT_CAPABILITY_SIGNING_KEY", "x" * 32)
    # Learning remains on its transitional API path until Gate D, so it has an
    # explicit independent model rather than falling back to REVIEW_MODEL.
    monkeypatch.setenv("RULE_LEARNING_MODEL", "openai/gpt-4.1-mini")

    # The API process performs only static validation: it intentionally has no
    # runner-control network or worker dispatch private key.
    worker.validate_worker_configuration(verify_native_runners=False)
