import pytest
from fastapi.testclient import TestClient

from service import oauth_api
from service.github_oauth import GitHubIdentity, GitHubOAuthError
from service.oauth_store import (
    APP_INSTALL_PURPOSE,
    CLI_LOGIN_PURPOSE,
    ConsumedOAuthState,
    UserRecord,
    generate_state,
)
from service.webhook_server import app

IDENTITY = GitHubIdentity(
    github_user_id=4242,
    login="octocat",
    avatar_url="https://avatars.example.com/octocat.png",
)


class FakeBackend:
    """Stands in for the database and GitHub, recording what the routes did."""

    def __init__(self):
        self.login_states: list[dict] = []
        self.install_states: list[dict] = []
        self.sessions: list[dict] = []
        self.installations: list[dict] = []
        self.exchanges: list[str] = []
        self.claimable: dict[tuple[str, str], ConsumedOAuthState] = {}
        self.exchange_error: Exception | None = None

    def allow(self, state: str, claimed: ConsumedOAuthState) -> None:
        self.claimable[(state, claimed.purpose)] = claimed

    # --- store doubles -------------------------------------------------
    def create_login_state(self, _conn, *, state, callback_port):
        self.login_states.append({"state": state, "callback_port": callback_port})

    def create_install_state(self, _conn, *, state, user_id):
        self.install_states.append({"state": state, "user_id": user_id})

    def consume_oauth_state(self, _conn, *, state, purpose):
        # Popping is what makes a replay fail the second time.
        return self.claimable.pop((state, purpose), None)

    def upsert_user(self, _conn, *, github_user_id, login, avatar_url):
        return UserRecord(
            id=7,
            github_user_id=github_user_id,
            login=login,
            avatar_url=avatar_url,
        )

    def create_session(self, _conn, *, user_id, token):
        self.sessions.append({"user_id": user_id, "token": token})

    def record_user_installation(self, _conn, *, user_id, github_installation_id):
        self.installations.append(
            {"user_id": user_id, "github_installation_id": github_installation_id}
        )

    # --- GitHub doubles ------------------------------------------------
    async def exchange_code_for_token(self, code):
        if self.exchange_error is not None:
            raise self.exchange_error
        self.exchanges.append(code)
        return "gho_" + "x" * 36

    async def fetch_authenticated_user(self, _token):
        return IDENTITY


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "Iv1.0123456789abcdef")
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)

    fake = FakeBackend()

    async def in_transaction(callback, /, **kwargs):
        return callback(object(), **kwargs)

    monkeypatch.setattr(oauth_api, "_in_transaction", in_transaction)
    for name in (
        "create_login_state",
        "create_install_state",
        "consume_oauth_state",
        "upsert_user",
        "create_session",
        "record_user_installation",
        "exchange_code_for_token",
        "fetch_authenticated_user",
    ):
        monkeypatch.setattr(oauth_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def test_cli_login_records_the_port_and_redirects_to_github(backend, client):
    nonce = generate_state()

    response = client.get(f"/auth/cli?port=53123&state={nonce}")

    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://github.com/login/oauth/authorize?"
    )
    assert f"state={nonce}" in response.headers["location"]
    assert "Iv1.0123456789abcdef" in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert backend.login_states == [{"state": nonce, "callback_port": 53123}]


def test_browser_initiated_login_mints_its_own_nonce_and_records_no_port(
    backend, client
):
    response = client.get("/auth/cli")

    assert response.status_code == 302
    assert backend.login_states[0]["callback_port"] is None
    assert len(backend.login_states[0]["state"]) >= 32


@pytest.mark.parametrize(
    "query",
    ["port=80", "port=1023", "port=65536", "port=abc", "port=", "state=short"],
)
def test_cli_login_rejects_out_of_range_ports_and_weak_nonces(backend, client, query):
    response = client.get(f"/auth/cli?{query}")

    assert response.status_code == 400
    assert backend.login_states == []


def test_callback_hands_the_session_token_to_the_recorded_loopback_port(
    backend, client
):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=1,
            purpose=CLI_LOGIN_PURPOSE,
            callback_port=53123,
            user_id=None,
        ),
    )

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("http://127.0.0.1:53123/callback?token=")
    assert backend.exchanges == ["abcd1234efgh"]

    minted = backend.sessions[0]
    assert minted["user_id"] == 7
    # The token reaches the CLI in the redirect and is stored only as a hash.
    assert location.endswith(minted["token"])


def test_callback_without_a_recorded_port_renders_the_all_set_page(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=2,
            purpose=CLI_LOGIN_PURPOSE,
            callback_port=None,
            user_id=None,
        ),
    )

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 200
    assert "You&#x27;re all set" in response.text
    assert "Signed in as octocat" in response.text
    assert backend.sessions[0]["token"] not in response.text
    assert backend.install_states[0]["user_id"] == 7


def test_forged_state_is_rejected_before_the_client_secret_is_spent(backend, client):
    response = client.get(
        f"/auth/github/callback?code=abcd1234efgh&state={generate_state()}"
    )

    assert response.status_code == 400
    assert backend.exchanges == []
    assert backend.sessions == []


def test_replayed_state_is_rejected_on_the_second_use(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=3,
            purpose=CLI_LOGIN_PURPOSE,
            callback_port=53123,
            user_id=None,
        ),
    )
    url = f"/auth/github/callback?code=abcd1234efgh&state={nonce}"

    assert client.get(url).status_code == 302
    replay = client.get(url)

    assert replay.status_code == 400
    assert len(backend.sessions) == 1


def test_callback_state_bound_to_an_install_cannot_be_used_to_sign_in(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=4,
            purpose=APP_INSTALL_PURPOSE,
            callback_port=None,
            user_id=7,
        ),
    )

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 400
    assert backend.exchanges == []


def test_callback_surfaces_a_failed_exchange_without_minting_a_session(
    backend, client
):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=5,
            purpose=CLI_LOGIN_PURPOSE,
            callback_port=53123,
            user_id=None,
        ),
    )
    backend.exchange_error = GitHubOAuthError("bad_verification_code")

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 502
    assert backend.sessions == []
    assert "bad_verification_code" not in response.text


def test_callback_reports_a_github_denial_without_touching_the_database(
    backend, client
):
    response = client.get("/auth/github/callback?error=access_denied")

    assert response.status_code == 400
    assert backend.exchanges == []


def test_setup_links_the_installation_to_the_user_that_started_it(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=6,
            purpose=APP_INSTALL_PURPOSE,
            callback_port=None,
            user_id=7,
        ),
    )

    response = client.get(f"/setup?installation_id=99887766&state={nonce}")

    assert response.status_code == 200
    assert "Connected" in response.text
    assert backend.installations == [
        {"user_id": 7, "github_installation_id": 99887766}
    ]


@pytest.mark.parametrize(
    "query",
    [
        "installation_id=99887766",
        "installation_id=99887766&state=" + "z" * 43,
        "installation_id=0&state=" + "z" * 43,
        "state=" + "z" * 43,
    ],
)
def test_setup_fails_closed_when_the_installer_cannot_be_identified(
    backend, client, query
):
    response = client.get(f"/setup?{query}")

    assert response.status_code == 400
    assert backend.installations == []


def test_sign_in_is_unavailable_when_the_oauth_client_is_not_configured(
    backend, client, monkeypatch
):
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)

    response = client.get("/auth/cli?port=53123")

    assert response.status_code == 503
    assert backend.login_states == []
