from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import relay_api
from service.relay_store import LeasedRelayDelivery, RelayNode

NODE = RelayNode(
    id=7,
    user_id=11,
    github_installation_id=12345,
    name="office-node",
    last_seen_at=None,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.state.github_webhook_secret = "webhook-secret"
    app.include_router(relay_api.router)
    return app


def test_pair_exchanges_a_single_use_code_for_a_node_token(monkeypatch):
    calls = []

    async def transaction(callback, **kwargs):
        calls.append((callback, kwargs))
        return NODE, "n" * 43

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    client = TestClient(_app())

    response = client.post(
        "/relay/v1/pair",
        json={"code": "p" * 43, "name": "office-node"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schemaVersion": "diffuse-relay-pair-v1",
        "nodeToken": "n" * 43,
        "nodeId": 7,
        "githubInstallationId": 12345,
    }
    assert calls[0][1] == {"code": "p" * 43, "node_name": "office-node"}


def test_node_leases_and_acknowledges_a_delivery(monkeypatch):
    delivery = LeasedRelayDelivery(
        id=91,
        provider="github",
        provider_delivery_id="delivery-guid",
        event_name="pull_request",
        payload=b'{"installation":{"id":12345}}',
        payload_sha256=hashlib.sha256(
            b'{"installation":{"id":12345}}'
        ).hexdigest(),
        attempt_count=1,
        leased_until=datetime.now(UTC) + timedelta(seconds=60),
    )

    async def transaction(callback, **kwargs):
        if callback is relay_api.authenticate_node:
            assert kwargs == {"token": "n" * 43}
            return NODE
        if callback is relay_api.lease_next_delivery:
            assert kwargs == {"node": NODE}
            return delivery
        if callback is relay_api.acknowledge_delivery:
            assert kwargs == {"node": NODE, "delivery_id": 91}
            return True
        raise AssertionError(callback)

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    client = TestClient(_app())
    headers = {"Authorization": "Bearer " + "n" * 43}

    leased = client.post("/relay/v1/deliveries/next", headers=headers)
    acknowledged = client.post("/relay/v1/deliveries/91/ack", headers=headers)

    assert leased.status_code == 200
    assert leased.json()["deliveryId"] == "delivery-guid"
    assert leased.json()["event"] == "pull_request"
    assert leased.json()["bodySha256"] == delivery.payload_sha256
    assert acknowledged.json() == {"status": "acknowledged", "id": 91}


def test_gateway_verifies_and_persists_before_acknowledging_github(monkeypatch):
    recorded = []

    async def transaction(callback, **kwargs):
        recorded.append((callback, kwargs))
        return True

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    client = TestClient(_app())
    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 12345},
        },
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        body,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-guid",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert recorded == [
        (
            relay_api.record_github_delivery,
            {
                "github_installation_id": 12345,
                "provider_delivery_id": "delivery-guid",
                "event_name": "pull_request",
                "payload": body,
            },
        )
    ]


def test_gateway_materializes_installation_ownership_without_queueing_node_work(
    monkeypatch,
):
    recorded = []

    async def transaction(callback, **kwargs):
        recorded.append((callback, kwargs))
        return True

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    client = TestClient(_app())
    payload = {
        "action": "created",
        "installation": {
            "id": 12345,
            "account": {
                "id": 99,
                "login": "octo-org",
                "type": "Organization",
            },
        },
        "sender": {"id": 42, "login": "octocat"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        body,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "installation",
            "X-GitHub-Delivery": "installation-guid",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 202
    assert recorded == [
        (relay_api.record_github_installation_event, {"payload": payload}),
    ]


def test_gateway_rejects_a_bad_github_signature(monkeypatch):
    async def transaction(_callback, **_kwargs):
        raise AssertionError("unverified webhook reached storage")

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    client = TestClient(_app())

    response = client.post(
        "/webhook/github",
        content=b'{"installation":{"id":12345}}',
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-guid",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )

    assert response.status_code == 401


def test_node_token_broker_uses_the_bound_installation(monkeypatch):
    async def transaction(callback, **kwargs):
        assert callback is relay_api.authenticate_node
        return NODE

    monkeypatch.setattr(relay_api, "_in_transaction", transaction)
    monkeypatch.setattr(
        relay_api,
        "mint_installation_token_for_installation",
        lambda installation_id: (
            "github-installation-token",
            3600,
        )
        if installation_id == 12345
        else None,
    )
    client = TestClient(_app())

    response = client.post(
        "/relay/v1/github/token",
        headers={"Authorization": "Bearer " + "n" * 43},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schemaVersion": "diffuse-relay-github-token-v1",
        "token": "github-installation-token",
        "expiresIn": 3600,
    }
