import hashlib

import pytest

from service.oauth_store import (
    generate_state,
    oauth_state_ttl_seconds,
    parse_installation_id,
    state_sha256,
    validate_state,
)


def test_generated_nonces_satisfy_their_own_validator():
    state = generate_state()

    assert validate_state(state) == state
    assert state_sha256(state) == hashlib.sha256(state.encode()).hexdigest()


def test_state_validation_rejects_low_entropy_and_unsafe_nonces():
    for invalid in ("", "short", "a" * 31, "a" * 129, "a" * 31 + "/", "a" * 31 + "%"):
        with pytest.raises(ValueError, match="URL-safe"):
            validate_state(invalid)


def test_installation_id_parsing_is_strict():
    assert parse_installation_id("42") == 42

    for invalid in ("", "0", "-1", "1.5", "abc", "1" * 20, " 42"):
        with pytest.raises(ValueError, match="installation_id"):
            parse_installation_id(invalid)


def test_ttl_overrides_are_bounded(monkeypatch):
    assert oauth_state_ttl_seconds() == 600

    monkeypatch.setenv("DIFFUSE_OAUTH_STATE_TTL_SECONDS", "120")
    assert oauth_state_ttl_seconds() == 120

    for invalid in ("0", "3601", "-5", "ten"):
        monkeypatch.setenv("DIFFUSE_OAUTH_STATE_TTL_SECONDS", invalid)
        with pytest.raises(ValueError, match="DIFFUSE_OAUTH_STATE_TTL_SECONDS"):
            oauth_state_ttl_seconds()
