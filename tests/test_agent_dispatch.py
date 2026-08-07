"""Signed native runner dispatches cannot be altered or replayed after expiry."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from service.agents.dispatch import (
    PRIVATE_KEY_VARIABLE,
    PUBLIC_KEY_VARIABLE,
    DispatchEnvelope,
    DispatchEnvelopeError,
    sign_dispatch,
    verify_dispatch,
)


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def dispatch_keys(monkeypatch):
    private = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        PRIVATE_KEY_VARIABLE,
        _base64url(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
    )
    monkeypatch.setenv(
        PUBLIC_KEY_VARIABLE,
        _base64url(
            private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ),
    )


def _envelope(expires_at: datetime) -> DispatchEnvelope:
    return DispatchEnvelope(
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability="opaque-capability",
        capability_id="capability-1",
        diff_text="diff --git a/a.py b/a.py",
        expires_at=expires_at,
    )


def test_dispatch_signature_binds_runtime_scope_and_expiry(dispatch_keys):
    token = sign_dispatch(_envelope(datetime.now(UTC) + timedelta(minutes=5)))

    actual = verify_dispatch(token)

    assert actual.runtime == "codex"
    assert actual.capability == "opaque-capability"


def test_dispatch_rejects_tampering_and_expired_envelopes(dispatch_keys):
    valid = sign_dispatch(_envelope(datetime.now(UTC) + timedelta(minutes=5)))
    prefix, body, signature = valid.split(".")
    tampered = f"{prefix}.{body[:-1]}x.{signature}"

    with pytest.raises(DispatchEnvelopeError):
        verify_dispatch(tampered)

    expired = sign_dispatch(_envelope(datetime.now(UTC) - timedelta(seconds=1)))
    with pytest.raises(DispatchEnvelopeError, match="expired"):
        verify_dispatch(expired)
