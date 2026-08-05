import hashlib
import hmac

import pytest
from fastapi import HTTPException

from service.github.api import verify_signature


def test_verify_signature_accepts_a_correct_hmac():
    body = b'{"action":"opened"}'
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    verify_signature(body, signature, secret)


def test_verify_signature_rejects_a_correctly_formatted_hmac_with_the_wrong_secret():
    body = b'{"action":"opened"}'
    signature = "sha256=" + hmac.new(
        b"wrong-secret", body, hashlib.sha256
    ).hexdigest()

    with pytest.raises(HTTPException) as raised:
        verify_signature(body, signature, "webhook-secret")

    assert raised.value.status_code == 401
    assert raised.value.detail == "Missing or invalid webhook signature"


def test_verify_signature_rejects_a_malformed_signature_prefix():
    with pytest.raises(HTTPException) as raised:
        verify_signature(b"{}", "sha256=bad", "webhook-secret")

    assert raised.value.status_code == 401


def test_verify_signature_rejects_non_ascii_signatures():
    with pytest.raises(HTTPException) as raised:
        verify_signature(b"{}", "sha256=" + ("ü" * 64), "webhook-secret")

    assert raised.value.status_code == 401


def test_verify_signature_requires_a_configured_secret():
    with pytest.raises(HTTPException) as raised:
        verify_signature(b"{}", "sha256=" + ("a" * 64), "")

    assert raised.value.status_code == 503
    assert raised.value.detail == "Webhook secret is not configured"
