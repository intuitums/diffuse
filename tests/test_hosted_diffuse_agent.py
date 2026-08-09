from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

HOSTED_ROOT = Path(__file__).parents[1] / "hosted"
if str(HOSTED_ROOT) not in sys.path:
    sys.path.insert(0, str(HOSTED_ROOT))

from diffuse_setup import app as hosted_app  # noqa: E402
from diffuse_setup import config as hosted_config  # noqa: E402
from diffuse_setup import store as hosted_store  # noqa: E402

from service.hosted import relay  # noqa: E402


class Request:
    def __init__(self, body: bytes):
        self.headers = {"content-length": str(len(body))}
        self._body = body

    async def body(self) -> bytes:
        return self._body


def test_hosted_webhook_verifies_before_storing(monkeypatch):
    body = json.dumps({"installation": {"id": 42}, "ref": "refs/heads/main"}).encode()
    monkeypatch.setattr(hosted_app, "webhook_secret", lambda: "webhook-secret")
    stored: list[dict[str, object]] = []
    monkeypatch.setattr(
        hosted_app,
        "record_webhook_event",
        lambda **kwargs: stored.append(kwargs) or True,
    )
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    response = asyncio.run(
        hosted_app.github_webhook(
            Request(body),
            x_github_event="push",
            x_github_delivery="delivery-1",
            x_hub_signature_256=signature,
        )
    )

    assert response == {"status": "accepted"}
    assert stored == [
        {
            "delivery_id": "delivery-1",
            "installation_id": 42,
            "event_name": "push",
            "payload": {"installation": {"id": 42}, "ref": "refs/heads/main"},
            "payload_sha256": hashlib.sha256(body).hexdigest(),
        }
    ]


def test_hosted_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(hosted_app, "webhook_secret", lambda: "webhook-secret")

    with pytest.raises(hosted_app.HTTPException, match="invalid webhook signature") as error:
        asyncio.run(
            hosted_app.github_webhook(
                Request(b'{"installation":{"id":42}}'),
                x_github_event="push",
                x_github_delivery="delivery-1",
                x_hub_signature_256="sha256=wrong",
            )
        )

    assert error.value.status_code == 401


def test_relay_signature_covers_the_complete_envelope():
    config = relay.RelayConfiguration(
        url="https://api.diffuse.website",
        instance_token="token",
        event_signing_key="event-key",
        poll_seconds=15,
    )
    payload = {"installation": {"id": 42}, "ref": "refs/heads/main"}
    signature = hmac.new(
        b"event-key",
        relay._canonical_event("delivery-1", "push", payload),
        hashlib.sha256,
    ).hexdigest()

    relay._verify_event(
        config,
        delivery_id="delivery-1",
        event_name="push",
        payload=payload,
        signature=signature,
    )
    with pytest.raises(relay.HostedRelayError, match="signature is invalid"):
        relay._verify_event(
            config,
            delivery_id="delivery-1",
            event_name="push",
            payload={"installation": {"id": 43}},
            signature=signature,
        )


def test_relay_requires_all_connection_values(monkeypatch):
    for name in (
        relay.RELAY_URL_VARIABLE,
        relay.RELAY_TOKEN_VARIABLE,
        relay.RELAY_EVENT_KEY_VARIABLE,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(relay.RELAY_URL_VARIABLE, "https://api.diffuse.website")

    with pytest.raises(ValueError, match="partial"):
        relay.configuration()


def test_hosted_database_url_uses_vercel_neon_value_when_no_override(monkeypatch):
    monkeypatch.delenv("DIFFUSE_SETUP_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon.example/diffuse")

    assert hosted_config.database_url() == "postgresql://neon.example/diffuse"


def test_hosted_database_url_allows_an_explicit_provider_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon.example/diffuse")
    monkeypatch.setenv(
        "DIFFUSE_SETUP_DATABASE_URL",
        "postgresql://provider.example/diffuse",
    )

    assert hosted_config.database_url() == "postgresql://provider.example/diffuse"


def test_hosted_webhook_insert_binds_each_placeholder_once(monkeypatch):
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, parameters=()):
            statements.append((query, parameters))
            assert query.count("%s") == len(parameters)

        def fetchone(self):
            return ("delivery-1",)

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_connection():
        yield Connection()

    monkeypatch.setattr(hosted_store, "connection", fake_connection)

    assert hosted_store.record_webhook_event(
        delivery_id="delivery-1",
        installation_id=42,
        event_name="push",
        payload={"installation": {"id": 42}},
        payload_sha256="hash",
    )
    assert len(statements) == 2
