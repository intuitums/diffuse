from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import contextmanager

import pytest
from diffuse.github import delivery_poller  # noqa: E402
from diffuse_relay import app as hosted_app  # noqa: E402
from diffuse_relay import config as hosted_config  # noqa: E402
from diffuse_relay import store as hosted_store  # noqa: E402


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


def test_delivery_signature_covers_the_complete_envelope():
    config = delivery_poller.DeliveryPollerConfiguration(
        url="https://api.diffuse.website",
        instance_token="token",
        event_signing_key="event-key",
        poll_seconds=15,
    )
    payload = {"installation": {"id": 42}, "ref": "refs/heads/main"}
    signature = hmac.new(
        b"event-key",
        delivery_poller._canonical_event("delivery-1", "push", payload),
        hashlib.sha256,
    ).hexdigest()

    delivery_poller._verify_event(
        config,
        delivery_id="delivery-1",
        event_name="push",
        payload=payload,
        signature=signature,
    )
    with pytest.raises(delivery_poller.GitHubDeliveryPollerError, match="signature is invalid"):
        delivery_poller._verify_event(
            config,
            delivery_id="delivery-1",
            event_name="push",
            payload={"installation": {"id": 43}},
            signature=signature,
        )


def test_delivery_poller_config_requires_complete_configuration(monkeypatch):
    for name in (
        delivery_poller.GITHUB_INTEGRATION_URL_VARIABLE,
        delivery_poller.GITHUB_INTEGRATION_TOKEN_VARIABLE,
        delivery_poller.GITHUB_DELIVERY_SIGNING_KEY_VARIABLE,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        delivery_poller.GITHUB_INTEGRATION_URL_VARIABLE,
        "https://api.diffuse.website",
    )

    with pytest.raises(ValueError, match="partial"):
        delivery_poller.configuration()


def test_delivery_poller_config_accepts_loopback_http_for_a_local_relay(monkeypatch):
    monkeypatch.delenv("DIFFUSE_ALLOW_PLAINTEXT_ORIGINS", raising=False)
    monkeypatch.setenv(delivery_poller.GITHUB_INTEGRATION_URL_VARIABLE, "http://localhost:8787")
    monkeypatch.setenv(delivery_poller.GITHUB_INTEGRATION_TOKEN_VARIABLE, "token")
    monkeypatch.setenv(delivery_poller.GITHUB_DELIVERY_SIGNING_KEY_VARIABLE, "key")

    assert delivery_poller.configuration().url == "http://localhost:8787"


def test_delivery_poller_config_rejects_plaintext_non_loopback(monkeypatch):
    monkeypatch.delenv("DIFFUSE_ALLOW_PLAINTEXT_ORIGINS", raising=False)
    monkeypatch.setenv(delivery_poller.GITHUB_INTEGRATION_URL_VARIABLE, "http://relay.example.com")
    monkeypatch.setenv(delivery_poller.GITHUB_INTEGRATION_TOKEN_VARIABLE, "token")
    monkeypatch.setenv(delivery_poller.GITHUB_DELIVERY_SIGNING_KEY_VARIABLE, "key")

    with pytest.raises(ValueError, match="https"):
        delivery_poller.configuration()


def test_hosted_database_url_uses_vercel_neon_value_when_no_override(monkeypatch):
    monkeypatch.delenv("DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon.example/diffuse")

    assert hosted_config.database_url() == "postgresql://neon.example/diffuse"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_public_url_accepts_loopback_http_for_a_local_relay(monkeypatch, host):
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL", f"http://{host}:8787")

    assert hosted_config.public_url() == f"http://{host}:8787"


@pytest.mark.parametrize(
    "value",
    [
        "http://relay.example.com",
        "https://api.diffuse.website/v1",
        "ftp://api.diffuse.website",
        "https://",
    ],
)
def test_public_url_rejects_non_origins(monkeypatch, value):
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_PUBLIC_URL", value)

    with pytest.raises(hosted_config.IntegrationConfigurationError):
        hosted_config.public_url()


def test_delivery_signing_key_is_sealed_at_rest_with_legacy_dual_read(monkeypatch):
    import base64

    kek = b"k" * 32
    monkeypatch.setenv(
        hosted_config.CREDENTIAL_KEK_VARIABLE,
        base64.urlsafe_b64encode(kek).decode().rstrip("="),
    )
    sealed = hosted_store._seal_delivery_signing_key("live-delivery-key")
    assert "live-delivery-key" not in sealed
    assert hosted_store._unseal_delivery_signing_key(sealed) == "live-delivery-key"
    assert hosted_store._unseal_delivery_signing_key("legacy-plaintext") == "legacy-plaintext"


def test_authenticate_instance_reseals_legacy_plaintext(monkeypatch):
    import base64

    kek = b"n" * 32
    monkeypatch.setenv(
        hosted_config.CREDENTIAL_KEK_VARIABLE,
        base64.urlsafe_b64encode(kek).decode().rstrip("="),
    )
    monkeypatch.setattr(hosted_store, "token_key", lambda: b"pepper" * 5 + b"xx")
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, parameters=()):
            statements.append((query, parameters))

        def fetchone(self):
            if "SELECT" in statements[-1][0]:
                return ("inst-1", 42, "legacy-plaintext-key")
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    @contextmanager
    def fake_connection():
        conn = Connection()
        yield conn
        conn.commit()

    monkeypatch.setattr(hosted_store, "connection", fake_connection)

    instance = hosted_store.authenticate_instance("instance-token")
    assert instance is not None
    assert instance.delivery_signing_key == "legacy-plaintext-key"
    update_params = next(
        params for query, params in statements if query.lstrip().startswith("UPDATE")
    )
    resealed = str(update_params[0])
    assert hosted_store.is_sealed(resealed)
    assert hosted_store._unseal_delivery_signing_key(resealed) == "legacy-plaintext-key"


def test_authenticate_instance_reseals_previous_kek_ciphertext(monkeypatch):
    import base64

    previous = b"p" * 32
    current = b"c" * 32
    monkeypatch.setenv(
        hosted_config.CREDENTIAL_KEK_VARIABLE,
        base64.urlsafe_b64encode(current).decode().rstrip("="),
    )
    monkeypatch.setenv(
        hosted_config.CREDENTIAL_KEK_PREVIOUS_VARIABLE,
        base64.urlsafe_b64encode(previous).decode().rstrip("="),
    )
    monkeypatch.setattr(hosted_store, "token_key", lambda: b"pepper" * 5 + b"xx")
    old_sealed = hosted_store.seal(
        "rotated-delivery-key",
        kek=previous,
        aad=hosted_config.DELIVERY_SIGNING_KEY_AAD,
    )
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, parameters=()):
            statements.append((query, parameters))

        def fetchone(self):
            if "SELECT" in statements[-1][0]:
                return ("inst-2", 7, old_sealed)
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    @contextmanager
    def fake_connection():
        conn = Connection()
        yield conn
        conn.commit()

    monkeypatch.setattr(hosted_store, "connection", fake_connection)

    instance = hosted_store.authenticate_instance("instance-token")
    assert instance is not None
    assert instance.delivery_signing_key == "rotated-delivery-key"
    update_params = next(
        params for query, params in statements if query.lstrip().startswith("UPDATE")
    )
    resealed = str(update_params[0])
    assert resealed.split(".")[2] == hosted_store.key_id_for(current)
    assert hosted_store._unseal_delivery_signing_key(resealed) == "rotated-delivery-key"


def test_hosted_database_url_allows_an_explicit_provider_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon.example/diffuse")
    monkeypatch.setenv(
        "DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL",
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


def test_ingest_event_admits_manual_review_comments(monkeypatch):
    request = object()
    event = object()
    monkeypatch.setattr(
        delivery_poller,
        "normalize_manual_review_request",
        lambda payload: request,
    )

    async def fake_fetch(manual_request, *, delivery_id):
        assert manual_request is request
        assert delivery_id == "delivery-manual"
        return event

    monkeypatch.setattr(delivery_poller, "fetch_manual_pull_request_event", fake_fetch)
    monkeypatch.setattr(
        delivery_poller,
        "enqueue_pull_request",
        lambda received, body: type("Result", (), {"accepted": True, "state": "queued"})(),
    )

    assert (
        delivery_poller.ingest_event(
            delivery_id="delivery-manual",
            event_name="issue_comment",
            payload={"action": "created", "comment": {"body": "@diffuse review"}},
        )
        == "accepted"
    )


def test_ingest_event_admits_converted_to_draft(monkeypatch):
    event = object()
    seen: list[tuple[dict, str, str]] = []

    def normalize(payload, *, delivery_id, action):
        seen.append((payload, delivery_id, action))
        return event

    monkeypatch.setattr(delivery_poller, "normalize_pull_request_event", normalize)
    monkeypatch.setattr(
        delivery_poller,
        "enqueue_pull_request",
        lambda received, body: type(
            "Result", (), {"accepted": True, "state": "queued"}
        )(),
    )
    payload = {"action": "converted_to_draft", "pull_request": {"draft": True}}

    assert (
        delivery_poller.ingest_event(
            delivery_id="delivery-draft",
            event_name="pull_request",
            payload=payload,
        )
        == "accepted"
    )
    assert seen == [(payload, "delivery-draft", "converted_to_draft")]


def test_ingest_event_records_review_feedback(monkeypatch):
    feedback = type("Feedback", (), {"repo_full_name": "acme/api", "delivery_id": "d1"})()
    monkeypatch.setattr(
        delivery_poller,
        "normalize_review_feedback_comment_event",
        lambda payload, delivery_id: feedback,
    )
    monkeypatch.setattr(
        delivery_poller,
        "normalize_review_conversation_event",
        lambda payload, delivery_id: None,
    )
    monkeypatch.setattr(delivery_poller, "record_review_feedback", lambda *_args: "recorded")

    assert (
        delivery_poller.ingest_event(
            delivery_id="d1",
            event_name="pull_request_review_comment",
            payload={"action": "created"},
        )
        == "recorded:review_feedback:recorded"
    )


def test_ingest_event_ignores_installation_lifecycle():
    assert (
        delivery_poller.ingest_event(
            delivery_id="d-install",
            event_name="installation",
            payload={"action": "deleted", "installation": {"id": 9}},
        )
        == "ignored:installation:deleted"
    )


def test_hosted_webhook_revokes_on_installation_deleted(monkeypatch):
    body = json.dumps(
        {"action": "deleted", "installation": {"id": 99}},
        separators=(",", ":"),
    ).encode()
    monkeypatch.setattr(hosted_app, "webhook_secret", lambda: "webhook-secret")
    calls: list[tuple[int, bool, bool]] = []
    monkeypatch.setattr(
        hosted_app,
        "set_installation_active",
        lambda installation_id, *, active, revoke_instances=False: calls.append(
            (installation_id, active, revoke_instances)
        )
        or True,
    )
    stored: list[object] = []
    monkeypatch.setattr(
        hosted_app,
        "record_webhook_event",
        lambda **kwargs: stored.append(kwargs) or True,
    )
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    response = asyncio.run(
        hosted_app.github_webhook(
            Request(body),
            x_github_event="installation",
            x_github_delivery="delivery-uninstall",
            x_hub_signature_256=signature,
        )
    )

    assert response == {"status": "installation_revoked"}
    assert calls == [(99, False, True)]
    assert stored == []


def test_hosted_webhook_suspends_without_revoking_instances(monkeypatch):
    body = json.dumps(
        {"action": "suspend", "installation": {"id": 7}},
        separators=(",", ":"),
    ).encode()
    monkeypatch.setattr(hosted_app, "webhook_secret", lambda: "webhook-secret")
    calls: list[tuple[int, bool, bool]] = []
    monkeypatch.setattr(
        hosted_app,
        "set_installation_active",
        lambda installation_id, *, active, revoke_instances=False: calls.append(
            (installation_id, active, revoke_instances)
        )
        or True,
    )
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    response = asyncio.run(
        hosted_app.github_webhook(
            Request(body),
            x_github_event="installation",
            x_github_delivery="delivery-suspend",
            x_hub_signature_256=signature,
        )
    )

    assert response == {"status": "installation_suspended"}
    assert calls == [(7, False, False)]


def test_callback_returns_html_for_browsers(monkeypatch):
    from diffuse_relay.github import GitHubInstallation
    from diffuse_relay.store import OAuthStateTarget

    monkeypatch.setattr(
        hosted_app,
        "consume_oauth_state",
        lambda state: OAuthStateTarget(installation_id=42, connect_session_id=None),
    )
    monkeypatch.setattr(
        hosted_app,
        "exchange_oauth_code",
        lambda code: (
            1,
            "owner",
            (GitHubInstallation(id=42, account_login="acme", account_type="Organization"),),
        ),
    )
    monkeypatch.setattr(hosted_app, "record_verified_installation", lambda *args, **kwargs: None)
    monkeypatch.setattr(hosted_app, "create_connection_code", lambda installation_id: "c" * 40)

    class CallbackRequest:
        headers = {"accept": "text/html,application/xhtml+xml"}

    response = asyncio.run(
        hosted_app.github_callback(CallbackRequest(), code="oauth-code", state="state")
    )
    assert isinstance(response, hosted_app.HTMLResponse)
    body = response.body.decode()
    assert "diffuse github connect" in body
    assert "Advanced: one-time connection code" in body
    assert "c" * 40 in body
    assert "--name" not in body.split("Advanced")[0]


def test_callback_returns_json_when_requested(monkeypatch):
    from diffuse_relay.github import GitHubInstallation
    from diffuse_relay.store import OAuthStateTarget

    monkeypatch.setattr(
        hosted_app,
        "consume_oauth_state",
        lambda state: OAuthStateTarget(installation_id=42, connect_session_id=None),
    )
    monkeypatch.setattr(
        hosted_app,
        "exchange_oauth_code",
        lambda code: (
            1,
            "owner",
            (GitHubInstallation(id=42, account_login="acme", account_type="Organization"),),
        ),
    )
    monkeypatch.setattr(hosted_app, "record_verified_installation", lambda *args, **kwargs: None)
    monkeypatch.setattr(hosted_app, "create_connection_code", lambda installation_id: "c" * 40)

    class CallbackRequest:
        headers = {"accept": "application/json"}

    response = asyncio.run(
        hosted_app.github_callback(CallbackRequest(), code="oauth-code", state="state")
    )
    assert isinstance(response, hosted_app.JSONResponse)
    assert json.loads(response.body.decode())["connection_code"] == "c" * 40


def test_connect_session_callback_auto_binds_single_installation(monkeypatch):
    from diffuse_relay.github import GitHubInstallation
    from diffuse_relay.store import InstanceCredentials, OAuthStateTarget

    session_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        hosted_app,
        "consume_oauth_state",
        lambda state: OAuthStateTarget(installation_id=None, connect_session_id=session_id),
    )
    monkeypatch.setattr(
        hosted_app,
        "exchange_oauth_code",
        lambda code: (
            9,
            "owner",
            (GitHubInstallation(id=42, account_login="acme", account_type="Organization"),),
        ),
    )
    monkeypatch.setattr(
        hosted_app,
        "get_pending_connect_session",
        lambda sid: hosted_store.ConnectSessionRow(
            session_id=sid,
            display_name="prod",
            status="pending",
            installation_id=None,
            candidate_installations=(),
            authorized_github_user_id=None,
            authorized_github_login=None,
            error_message=None,
            expires_at="2026-08-10T00:00:00+00:00",
        ),
    )
    completed: list[object] = []

    def fake_complete(sid, **kwargs):
        completed.append((sid, kwargs))
        return InstanceCredentials(
            instance_id="inst-1",
            instance_token="token",
            delivery_signing_key="signing",
            installation_id=42,
        )

    monkeypatch.setattr(hosted_app, "complete_connect_session", fake_complete)

    class CallbackRequest:
        headers = {"accept": "text/html"}
        cookies = {}

    response = asyncio.run(
        hosted_app.github_callback(CallbackRequest(), code="oauth-code", state="state")
    )
    assert isinstance(response, hosted_app.HTMLResponse)
    assert "close this window" in response.body.decode().lower()
    assert completed[0][0] == session_id
    assert completed[0][1]["installation_id"] == 42


def test_connect_sessions_create_returns_browser_url(monkeypatch):
    monkeypatch.setattr(
        hosted_app,
        "create_connect_session",
        lambda *, display_name: hosted_store.ConnectSessionCreated(
            session_id="11111111-1111-1111-1111-111111111111",
            poll_secret="poll-secret",
            expires_in_seconds=900,
        ),
    )
    monkeypatch.setattr(hosted_app, "public_url", lambda: "https://api.diffuse.website")
    payload = asyncio.run(
        hosted_app.connect_sessions_create(hosted_app.ConnectSessionRequest(display_name="prod"))
    )
    assert payload["browser_url"].endswith(
        "/auth/github/connect/11111111-1111-1111-1111-111111111111"
    )
    assert payload["poll_secret"] == "poll-secret"


def test_instances_me_reports_not_ready_when_installation_inactive(monkeypatch):
    monkeypatch.setattr(
        hosted_app,
        "instance_status",
        lambda token: hosted_store.InstanceStatus(
            instance_id="inst-1",
            installation_id=42,
            display_name="prod",
            installation_active=False,
            pending_events=3,
            created_at="2026-08-09T00:00:00+00:00",
            updated_at="2026-08-09T00:00:00+00:00",
        ),
    )
    payload = asyncio.run(hosted_app.instances_me(authorization="Bearer token"))
    assert payload["ready"] is False
    assert payload["status"] == "not_ready"
    assert payload["pending_events"] == 3
    assert "suspended" in payload["diagnostic"]


def test_installation_token_refuses_inactive_installation(monkeypatch):
    monkeypatch.setattr(
        hosted_app,
        "instance_status",
        lambda token: hosted_store.InstanceStatus(
            instance_id="inst-1",
            installation_id=42,
            display_name="prod",
            installation_active=False,
            pending_events=0,
            created_at="2026-08-09T00:00:00+00:00",
            updated_at="2026-08-09T00:00:00+00:00",
        ),
    )
    with pytest.raises(hosted_app.HTTPException, match="suspended or inactive") as error:
        asyncio.run(hosted_app.installation_token(authorization="Bearer token"))
    assert error.value.status_code == 403
