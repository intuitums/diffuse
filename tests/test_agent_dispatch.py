"""Signed native runner dispatches cannot be altered or replayed after expiry."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from service.agents.dispatch import (
    MAX_DISPATCH_DIFF_CHARS,
    PRIVATE_KEY_VARIABLE,
    PUBLIC_KEY_VARIABLE,
    DispatchEnvelope,
    DispatchEnvelopeError,
    sign_dispatch,
    verify_dispatch,
)
from service.agents.transport_secret import TRANSPORT_SECRET_VARIABLE
from service.crypto.sealed_secret import is_sealed
from service.review.workspace import DEFAULT_WORKSPACE_LIMITS, SourceArtifact


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
    monkeypatch.setenv(TRANSPORT_SECRET_VARIABLE, _base64url(os.urandom(32)))


def _envelope(expires_at: datetime) -> DispatchEnvelope:
    return DispatchEnvelope(
        session_id="a32b1c5d-3c15-4462-a9fe-f191775b3459",
        runtime="codex",
        capability="opaque-capability",
        capability_id="capability-1",
        diff_text="diff --git a/a.py b/a.py",
        source_artifact=SourceArtifact(b"test source archive", manifest_digest="0" * 64),
        expires_at=expires_at,
    )


def test_dispatch_signature_binds_runtime_scope_and_expiry(dispatch_keys):
    token = sign_dispatch(_envelope(datetime.now(UTC) + timedelta(minutes=5)))

    actual = verify_dispatch(token)

    assert actual.runtime == "codex"
    assert actual.capability == "opaque-capability"
    assert actual.source_artifact.archive == b"test source archive"


def test_dispatch_envelope_does_not_embed_plaintext_capability(dispatch_keys):
    token = sign_dispatch(_envelope(datetime.now(UTC) + timedelta(minutes=5)))
    _prefix, body, _signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))

    assert "opaque-capability" not in token
    assert is_sealed(payload["capability"])
    assert payload["capability_id"] == "capability-1"


def test_dispatch_rejects_tampering_and_expired_envelopes(dispatch_keys):
    valid = sign_dispatch(_envelope(datetime.now(UTC) + timedelta(minutes=5)))
    prefix, body, signature = valid.split(".")
    tampered = f"{prefix}.{body[:-1]}x.{signature}"

    with pytest.raises(DispatchEnvelopeError):
        verify_dispatch(tampered)

    expired = sign_dispatch(_envelope(datetime.now(UTC) - timedelta(seconds=1)))
    with pytest.raises(DispatchEnvelopeError, match="expired"):
        verify_dispatch(expired)


def test_dispatch_requires_the_canonical_workspace_digest(dispatch_keys):
    envelope = _envelope(datetime.now(UTC) + timedelta(minutes=5))

    with pytest.raises(DispatchEnvelopeError, match="workspace manifest digest"):
        sign_dispatch(
            replace(
                envelope,
                source_artifact=SourceArtifact(b"source without a manifest"),
            )
        )


def test_dispatch_refuses_a_diff_outside_the_native_review_window(dispatch_keys):
    envelope = _envelope(datetime.now(UTC) + timedelta(minutes=5))

    with pytest.raises(DispatchEnvelopeError, match="diff exceeds"):
        sign_dispatch(replace(envelope, diff_text="x" * (MAX_DISPATCH_DIFF_CHARS + 1)))


def test_dispatch_refuses_an_oversized_source_archive_before_encoding(dispatch_keys):
    envelope = _envelope(datetime.now(UTC) + timedelta(minutes=5))

    with pytest.raises(DispatchEnvelopeError, match="source archive exceeds"):
        sign_dispatch(
            replace(
                envelope,
                source_artifact=SourceArtifact(
                    b"x" * (DEFAULT_WORKSPACE_LIMITS.max_archive_bytes + 1),
                    manifest_digest="0" * 64,
                ),
            )
        )
