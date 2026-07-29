import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import oauth_api
from service.github_app import GitHubInstallation
from service.github_oauth import GitHubIdentity, GitHubOAuthError
from service.oauth_store import (
    APP_INSTALL_PURPOSE,
    GITHUB_INSTALL_AUTH_PURPOSE,
    ConsumedOAuthState,
    UserRecord,
    generate_state,
)
from service.relay_store import InstallationOwnershipError
from service.webhook_server import app as review_node_app

app = FastAPI()
app.include_router(oauth_api.router)

IDENTITY = GitHubIdentity(
    github_user_id=4242,
    login="octocat",
    avatar_url="https://avatars.example.com/octocat.png",
)


class FakeBackend:
    """Stands in for the database and GitHub, recording what the routes did."""

    def __init__(self):
        self.install_auth_states: list[dict] = []
        self.install_states: list[dict] = []
        # Every store call the routes make, in order, by name. Asserting on this
        # catches a persisted linkage no matter which function writes it.
        self.db_calls: list[str] = []
        self.exchanges: list[str] = []
        self.claimable: dict[tuple[str, str], ConsumedOAuthState] = {}
        self.exchange_error: Exception | None = None
        self.installation_owner_user_id = 7
        self.installation_links: list[dict] = []

    def allow(self, state: str, claimed: ConsumedOAuthState) -> None:
        self.claimable[(state, claimed.purpose)] = claimed

    # --- store doubles -------------------------------------------------
    def create_install_auth_state(self, _conn, *, state):
        self.install_auth_states.append({"state": state})

    def create_install_state(self, _conn, *, state, user_id):
        self.install_states.append({"state": state, "user_id": user_id})

    def consume_oauth_state(self, _conn, *, state, purpose):
        # Popping is what makes a replay fail the second time.
        return self.claimable.pop((state, purpose), None)

    def load_oauth_state(self, _conn, *, state, purpose):
        return self.claimable.get((state, purpose))

    def upsert_user(self, _conn, *, github_user_id, login, avatar_url):
        return UserRecord(
            id=7,
            github_user_id=github_user_id,
            login=login,
            avatar_url=avatar_url,
        )

    def authorize_installation_user(
        self,
        _conn,
        *,
        user_id,
        github_installation_id,
    ):
        if user_id != self.installation_owner_user_id:
            raise InstallationOwnershipError("not the signed installer")
        return "octo-org"

    def record_user_installation(
        self,
        _conn,
        *,
        user_id,
        github_installation_id,
        account_login,
    ):
        self.installation_links.append(
            {
                "user_id": user_id,
                "github_installation_id": github_installation_id,
                "account_login": account_login,
            }
        )

    def create_pairing_code(
        self,
        _conn,
        *,
        user_id,
        github_installation_id,
    ):
        return "p" * 43

    # --- GitHub doubles ------------------------------------------------
    async def exchange_code_for_token(self, code):
        if self.exchange_error is not None:
            raise self.exchange_error
        self.exchanges.append(code)
        return "gho_" + "x" * 36

    async def fetch_authenticated_user(self, _token):
        return IDENTITY

    def verify_installation(self, installation_id):
        return GitHubInstallation(id=installation_id, account_login="octo-org")


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "Iv1.0123456789abcdef")
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")
    monkeypatch.setenv(
        "DIFFUSE_PUBLIC_URL",
        "https://integrations.diffuse.example",
    )
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)

    fake = FakeBackend()

    async def in_transaction(callback, /, **kwargs):
        fake.db_calls.append(getattr(callback, "__name__", repr(callback)))
        return callback(object(), **kwargs)

    monkeypatch.setattr(oauth_api, "_in_transaction", in_transaction)
    for name in (
        "create_install_auth_state",
        "create_install_state",
        "load_oauth_state",
        "consume_oauth_state",
        "upsert_user",
        "authorize_installation_user",
        "record_user_installation",
        "create_pairing_code",
        "exchange_code_for_token",
        "fetch_authenticated_user",
        "verify_installation",
    ):
        monkeypatch.setattr(oauth_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def test_github_installer_auth_is_not_mounted_on_the_review_node():
    review_client = TestClient(review_node_app, follow_redirects=False)

    assert review_client.get("/auth/github").status_code == 404
    assert review_client.get("/auth/github/callback").status_code == 404
    assert review_client.get("/setup").status_code == 404


def test_github_install_auth_mints_state_and_redirects_to_github(backend, client):
    response = client.get("/auth/github")

    assert response.status_code == 302
    location = response.headers["location"]
    nonce = backend.install_auth_states[0]["state"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert f"state={nonce}" in location
    assert "Iv1.0123456789abcdef" in location
    assert response.headers["cache-control"] == "no-store"
    assert len(nonce) >= 32


def test_github_install_auth_ignores_caller_supplied_state_and_port(backend, client):
    supplied = generate_state()

    response = client.get(f"/auth/github?state={supplied}&port=53123")

    assert response.status_code == 302
    recorded = backend.install_auth_states[0]["state"]
    assert recorded != supplied
    assert f"state={recorded}" in response.headers["location"]


def test_callback_creates_an_install_state_and_renders_the_all_set_page(
    backend, client
):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=2,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            user_id=None,
        ),
    )

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 200
    assert "You&#x27;re all set" in response.text
    assert "Signed in as octocat" in response.text
    assert backend.exchanges == ["abcd1234efgh"]
    assert backend.install_states[0]["user_id"] == 7


def test_forged_state_is_rejected_before_the_client_secret_is_spent(backend, client):
    response = client.get(
        f"/auth/github/callback?code=abcd1234efgh&state={generate_state()}"
    )

    assert response.status_code == 400
    assert backend.exchanges == []


def test_replayed_state_is_rejected_on_the_second_use(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=3,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            user_id=None,
        ),
    )
    url = f"/auth/github/callback?code=abcd1234efgh&state={nonce}"

    assert client.get(url).status_code == 200
    replay = client.get(url)

    assert replay.status_code == 400
    assert len(backend.install_states) == 1


def test_callback_state_bound_to_an_install_cannot_be_used_to_sign_in(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=4,
            purpose=APP_INSTALL_PURPOSE,
            user_id=7,
        ),
    )

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 400
    assert backend.exchanges == []


def test_callback_surfaces_a_failed_exchange_without_creating_install_state(
    backend, client
):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=5,
            purpose=GITHUB_INSTALL_AUTH_PURPOSE,
            user_id=None,
        ),
    )
    backend.exchange_error = GitHubOAuthError("bad_verification_code")

    response = client.get(f"/auth/github/callback?code=abcd1234efgh&state={nonce}")

    assert response.status_code == 502
    assert backend.install_states == []
    assert "bad_verification_code" not in response.text


def test_callback_reports_a_github_denial_without_touching_the_database(
    backend, client
):
    response = client.get("/auth/github/callback?error=access_denied")

    assert response.status_code == 400
    assert backend.exchanges == []


def test_setup_verifies_links_and_issues_a_node_pairing_code(backend, client):
    nonce = generate_state()
    backend.allow(
        nonce,
        ConsumedOAuthState(
            id=6,
            purpose=APP_INSTALL_PURPOSE,
            user_id=7,
        ),
    )

    response = client.get(f"/setup?installation_id=99887766&state={nonce}")

    assert response.status_code == 200
    assert "Installed" in response.text
    assert "octo-org" in response.text
    assert (
        "diffuse relay pair --gateway https://integrations.diffuse.example --code"
        in response.text
    )
    assert backend.installation_links == [
        {
            "user_id": 7,
            "github_installation_id": 99887766,
            "account_login": "octo-org",
        }
    ]
    assert backend.db_calls == [
        "load_oauth_state",
        "_link_and_issue_pairing_code",
    ]


def test_setup_cannot_be_used_to_claim_another_users_installation(backend, client):
    """A valid state proves who started an install, not *which* install.

    `installation_id` is caller-controlled and ids are sequential, so an
    attacker who signs in normally could otherwise attach a victim's org
    installation to their own account — poisoning the table tenancy will read.
    """
    attacker_nonce = generate_state()
    backend.allow(
        attacker_nonce,
        ConsumedOAuthState(
            id=7,
            purpose=APP_INSTALL_PURPOSE,
            user_id=1234,  # the attacker's own, legitimately obtained, user id
        ),
    )
    victim_installation = 99887766

    response = client.get(
        f"/setup?installation_id={victim_installation}&state={attacker_nonce}"
    )

    assert response.status_code == 409
    assert backend.installation_links == []
    assert backend.db_calls == [
        "load_oauth_state",
        "_link_and_issue_pairing_code",
    ]
    # Ownership is checked before the state is consumed, so GitHub's signed
    # installation event can arrive and this same callback can be retried.
    assert (attacker_nonce, APP_INSTALL_PURPOSE) in backend.claimable


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
    assert [call for call in backend.db_calls if call != "load_oauth_state"] == []


def test_sign_in_is_unavailable_when_the_oauth_client_is_not_configured(
    backend, client, monkeypatch
):
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)

    response = client.get("/auth/github")

    assert response.status_code == 503
    assert backend.install_auth_states == []
