from __future__ import annotations

import base64
import hashlib

import anyio
import httpx
import pytest

from service import relay_client


def _response(body: bytes, **overrides) -> httpx.Response:
    payload = {
        "id": 9,
        "provider": "github",
        "deliveryId": "delivery-guid",
        "event": "pull_request",
        "bodyBase64": base64.b64encode(body).decode(),
        "bodySha256": hashlib.sha256(body).hexdigest(),
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def test_delivery_payload_verifies_the_body_digest():
    body = b'{"installation":{"id":12345}}'

    assert relay_client._delivery_payload(_response(body)) == (
        9,
        "delivery-guid",
        "pull_request",
        body,
    )


def test_delivery_payload_rejects_tampering():
    with pytest.raises(relay_client.RelayClientError, match="digest"):
        relay_client._delivery_payload(
            _response(b"real", bodySha256=hashlib.sha256(b"other").hexdigest())
        )


def test_pull_once_delivers_locally_then_acknowledges(monkeypatch):
    body = b'{"installation":{"id":12345}}'
    calls: list[tuple[str, dict]] = []
    responses = [
        _response(body, event="installation"),
        httpx.Response(200, json={"status": "ignored"}),
        httpx.Response(200, json={"status": "acknowledged"}),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return responses.pop(0)

    monkeypatch.setenv("DIFFUSE_RELAY_URL", "https://integrations.diffuse.example")
    monkeypatch.setenv("DIFFUSE_RELAY_TOKEN", "n" * 43)
    monkeypatch.setattr(relay_client.httpx, "AsyncClient", FakeClient)

    assert anyio.run(relay_client.pull_once)
    assert calls[0][0].endswith("/relay/v1/deliveries/next")
    assert calls[1][0] == "/webhook/github"
    assert calls[1][1]["headers"]["X-Diffuse-Relay-Signature"].startswith("sha256=")
    assert calls[2][0].endswith("/relay/v1/deliveries/9/ack")
    assert responses == []
