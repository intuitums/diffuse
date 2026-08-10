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

from github_integration import app as hosted_app  # noqa: E402
from github_integration import config as hosted_config  # noqa: E402
from github_integration import store as hosted_store  # noqa: E402

from service.github import delivery_poller  # noqa: E402


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


def test_delivery_poller_requires_all_connection_values(monkeypatch):
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


def test_hosted_database_url_uses_vercel_neon_value_when_no_override(monkeypatch):
    monkeypatch.delenv("DIFFUSE_GITHUB_INTEGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon.example/diffuse")

    assert hosted_config.database_url() == "postgresql://neon.example/diffuse"


def test_event_signing_key_is_sealed_at_rest_with_legacy_dual_read(monkeypatch):
    import base64

    kek = b"k" * 32
    monkeypatch.setenv(
        hosted_config.CREDENTIAL_KEK_VARIABLE,
        base64.urlsafe_b64encode(kek).decode().rstrip("="),
    )
    sealed = hosted_store._seal_event_signing_key("live-delivery-key")
    assert "live-delivery-key" not in sealed
    assert hosted_store._unseal_event_signing_key(sealed) == "live-delivery-key"
    assert hosted_store._unseal_event_signing_key("legacy-plaintext") == "legacy-plaintext"


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
    assert instance.event_signing_key == "legacy-plaintext-key"
    update_params = next(
        params for query, params in statements if query.lstrip().startswith("UPDATE")
    )
    resealed = str(update_params[0])
    assert hosted_store.is_sealed(resealed)
    assert hosted_store._unseal_event_signing_key(resealed) == "legacy-plaintext-key"


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
        aad=hosted_config.EVENT_SIGNING_KEY_AAD,
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
    assert instance.event_signing_key == "rotated-delivery-key"
    update_params = next(
        params for query, params in statements if query.lstrip().startswith("UPDATE")
    )
    resealed = str(update_params[0])
    assert resealed.split(".")[2] == hosted_store.key_id_for(current)
    assert hosted_store._unseal_event_signing_key(resealed) == "rotated-delivery-key"


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


def test_setup_callback_without_cli_tells_operator_to_run_connect(monkeypatch):
    monkeypatch.setattr(
        hosted_app,
        "consume_oauth_state",
        lambda _state: hosted_store.OAuthState(installation_id=42, connect_session_id=None),
    )
    monkeypatch.setattr(
        hosted_app,
        "exchange_oauth_code",
        lambda _code: (
            7,
            "octocat",
            (hosted_app.GitHubInstallationSummary(42, "acme", "Organization"),),
        ),
    )
    recorded: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        hosted_app,
        "record_verified_installation",
        lambda installation_id, *, github_user_id, github_login: recorded.append(
            (installation_id, github_user_id, github_login)
        ),
    )

    class _Request:
        headers = {"accept": "text/html"}

    response = asyncio.run(hosted_app.github_callback(_Request(), code="oauth-code", state="state"))
    assert response.status_code == 200
    assert "diffuse github connect" in response.body.decode()
    assert "connection code" not in response.body.decode().lower()
    assert recorded == [(42, 7, "octocat")]


def test_connect_session_claim_returns_credentials_once(monkeypatch):
    created = hosted_store.ConnectSessionCreated(
        session_id="sess-1",
        poll_token="poll-secret",
        browser_url="https://api.diffuse.website/connect/sess-1",
        expires_in_seconds=900,
    )
    monkeypatch.setattr(hosted_app, "public_url", lambda: "https://api.diffuse.website")
    monkeypatch.setattr(
        hosted_app,
        "create_connect_session",
        lambda **kwargs: created,
    )

    create_response = asyncio.run(
        hosted_app.connect_sessions_create(
            hosted_app.ConnectSessionRequest(display_name="prod")
        )
    )
    assert create_response["browser_url"].endswith("/connect/sess-1")
    assert create_response["poll_token"] == "poll-secret"

    credentials = hosted_store.InstanceCredentials(
        instance_id="inst-1",
        instance_token="token-secret",
        event_signing_key="signing-secret",
        installation_id=42,
    )
    claims = iter([("pending", None), ("ready", credentials), ("consumed", None)])
    monkeypatch.setattr(
        hosted_app,
        "claim_connect_session",
        lambda session_id, *, poll_token: next(claims),
    )

    pending = asyncio.run(
        hosted_app.connect_sessions_poll("sess-1", authorization="Bearer poll-secret")
    )
    assert pending == {"status": "pending"}
    ready = asyncio.run(
        hosted_app.connect_sessions_poll("sess-1", authorization="Bearer poll-secret")
    )
    assert ready["instance_token"] == "token-secret"
    assert ready["secrets_shown_once"] is True
    consumed = asyncio.run(
        hosted_app.connect_sessions_poll("sess-1", authorization="Bearer poll-secret")
    )
    assert consumed == {"status": "consumed"}


def test_connect_browser_starts_oauth_for_session(monkeypatch):
    monkeypatch.setattr(
        hosted_app,
        "get_connect_session",
        lambda _session_id: hosted_store.ConnectSession(
            id="sess-1",
            display_name="prod",
            status="pending",
            github_user_id=None,
            github_login=None,
            allowed_installation_ids=(),
            github_installation_id=None,
            error_message=None,
            expired=False,
        ),
    )
    monkeypatch.setattr(
        hosted_app,
        "create_oauth_state",
        lambda installation_id=None, *, connect_session_id=None: "oauth-state",
    )
    monkeypatch.setattr(
        hosted_app,
        "oauth_authorize_url",
        lambda state: f"https://github.com/login/oauth/authorize?state={state}",
    )

    response = asyncio.run(hosted_app.connect_browser("sess-1"))
    assert response.status_code == 302
    assert response.headers["location"].endswith("state=oauth-state")


def test_github_app_slug_config(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "diffuse-review-agent")
    assert hosted_config.github_app_slug() == "diffuse-review-agent"
    monkeypatch.setenv("GITHUB_APP_SLUG", "bad slug")
    with pytest.raises(hosted_config.HostedConfigurationError, match="GITHUB_APP_SLUG"):
        hosted_config.github_app_slug()
