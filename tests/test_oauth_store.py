import hashlib

import pytest

from service.oauth_store import (
    build_loopback_redirect,
    generate_session_token,
    generate_state,
    oauth_state_ttl_seconds,
    parse_callback_port,
    parse_installation_id,
    session_token_sha256,
    session_ttl_seconds,
    state_sha256,
    validate_callback_port,
    validate_session_token,
    validate_state,
)


def test_generated_nonces_and_tokens_satisfy_their_own_validators():
    state = generate_state()
    token = generate_session_token()

    assert validate_state(state) == state
    assert validate_session_token(token) == token
    assert state_sha256(state) == hashlib.sha256(state.encode()).hexdigest()
    assert session_token_sha256(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in session_token_sha256(token)


def test_state_validation_rejects_low_entropy_and_unsafe_nonces():
    for invalid in ("", "short", "a" * 31, "a" * 129, "a" * 31 + "/", "a" * 31 + "%"):
        with pytest.raises(ValueError, match="URL-safe"):
            validate_state(invalid)


def test_callback_port_is_restricted_to_the_unprivileged_loopback_range():
    assert validate_callback_port(1024) == 1024
    assert validate_callback_port(65535) == 65535
    assert parse_callback_port("53123") == 53123

    for invalid in (0, 80, 443, 1023, 65536, -1):
        with pytest.raises(ValueError, match="Callback port"):
            validate_callback_port(invalid)

    # `True` is an int in Python; a boolean must not slip through as port 1.
    with pytest.raises(ValueError, match="must be an integer"):
        validate_callback_port(True)

    for invalid in ("", " 8080", "8080 ", "0x1f90", "+8080", "80.80", "123456"):
        with pytest.raises(ValueError, match="Callback port"):
            parse_callback_port(invalid)


def test_loopback_redirect_rejects_any_non_loopback_target():
    token = "t" * 43

    assert build_loopback_redirect(port=53123, token=token) == (
        f"http://127.0.0.1:53123/callback?token={token}"
    )
    assert build_loopback_redirect(port=53123, token=token, host="localhost") == (
        f"http://localhost:53123/callback?token={token}"
    )

    for host in (
        "evil.example.com",
        "127.0.0.1.evil.example.com",
        "0.0.0.0",
        "127.1",
        "[::1]",
        "127.0.0.1:1",
        "",
    ):
        with pytest.raises(ValueError, match="127.0.0.1 or localhost"):
            build_loopback_redirect(port=53123, token=token, host=host)


def test_loopback_redirect_encodes_the_token_and_validates_it():
    with pytest.raises(ValueError, match="visible ASCII"):
        build_loopback_redirect(port=53123, token="too-short")

    # Generated tokens are URL-safe, but the encoder must not let a token that
    # is merely "visible ASCII" smuggle extra query parameters into the CLI.
    encoded = build_loopback_redirect(port=53123, token="a" * 40 + "&admin=1")
    assert encoded.endswith("/callback?token=" + "a" * 40 + "%26admin%3D1")


def test_installation_id_parsing_is_strict():
    assert parse_installation_id("42") == 42

    for invalid in ("", "0", "-1", "1.5", "abc", "1" * 20, " 42"):
        with pytest.raises(ValueError, match="installation_id"):
            parse_installation_id(invalid)


def test_ttl_overrides_are_bounded(monkeypatch):
    assert oauth_state_ttl_seconds() == 600
    assert session_ttl_seconds() == 30 * 24 * 60 * 60

    monkeypatch.setenv("DIFFUSE_OAUTH_STATE_TTL_SECONDS", "120")
    assert oauth_state_ttl_seconds() == 120

    for invalid in ("0", "3601", "-5", "ten"):
        monkeypatch.setenv("DIFFUSE_OAUTH_STATE_TTL_SECONDS", invalid)
        with pytest.raises(ValueError, match="DIFFUSE_OAUTH_STATE_TTL_SECONDS"):
            oauth_state_ttl_seconds()
