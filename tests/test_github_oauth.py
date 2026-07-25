import httpx
import pytest

from service.github_oauth import (
    GitHubOAuthConfigurationError,
    GitHubOAuthError,
    build_app_install_url,
    build_authorize_url,
    exchange_code_for_token,
    fetch_authenticated_user,
    oauth_client_secret,
)

SECRET = "s" * 40


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    path = tmp_path / "app-client-secret"
    path.write_text(f"{SECRET}\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET_FILE", str(path))
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "Iv1.0123456789abcdef")
    monkeypatch.setenv("GITHUB_WEB_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    return path


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_client_secret_is_read_from_the_file_and_trimmed(secret_file):
    assert oauth_client_secret() == SECRET


def test_client_secret_file_must_exist_and_be_privately_writable(
    secret_file, tmp_path, monkeypatch
):
    secret_file.chmod(0o646)
    with pytest.raises(GitHubOAuthConfigurationError, match="world-writable"):
        oauth_client_secret()

    secret_file.chmod(0o600)
    secret_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(GitHubOAuthConfigurationError, match="usable secret"):
        oauth_client_secret()

    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET_FILE", str(tmp_path / "absent"))
    with pytest.raises(GitHubOAuthConfigurationError, match="unreadable"):
        oauth_client_secret()


def test_configuration_errors_never_disclose_the_secret(secret_file):
    secret_file.chmod(0o646)
    with pytest.raises(GitHubOAuthConfigurationError) as raised:
        oauth_client_secret()
    assert SECRET not in str(raised.value)


def test_authorize_and_install_urls_are_built_from_the_configured_host(
    secret_file, monkeypatch
):
    url = build_authorize_url(state="n" * 43)
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=Iv1.0123456789abcdef" in url
    assert f"state={'n' * 43}" in url

    assert build_app_install_url(state="n" * 43) is None

    monkeypatch.setenv("GITHUB_APP_SLUG", "diffuse-review")
    assert build_app_install_url(state="n" * 43) == (
        f"https://github.com/apps/diffuse-review/installations/new?state={'n' * 43}"
    )


@pytest.mark.anyio
async def test_code_exchange_posts_the_secret_and_returns_the_access_token(
    secret_file,
):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["accept"] = request.headers["accept"]
        return httpx.Response(200, json={"access_token": "gho_" + "x" * 36})

    async with _client(handler) as client:
        token = await exchange_code_for_token("abcd1234efgh", client=client)

    assert token == "gho_" + "x" * 36
    assert seen["url"] == "https://github.com/login/oauth/access_token"
    assert seen["accept"] == "application/json"
    assert f"client_secret={SECRET}" in seen["body"]
    assert "code=abcd1234efgh" in seen["body"]


@pytest.mark.anyio
async def test_code_exchange_surfaces_github_errors_without_echoing_the_body(
    secret_file,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "bad_verification_code", "client_secret": SECRET},
        )

    async with _client(handler) as client:
        with pytest.raises(GitHubOAuthError) as raised:
            await exchange_code_for_token("abcd1234efgh", client=client)

    assert "bad_verification_code" in str(raised.value)
    assert SECRET not in str(raised.value)


@pytest.mark.anyio
async def test_code_exchange_rejects_a_missing_or_malformed_token(secret_file):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    async with _client(handler) as client:
        with pytest.raises(GitHubOAuthError, match="no access token"):
            await exchange_code_for_token("abcd1234efgh", client=client)


@pytest.mark.anyio
async def test_code_exchange_refuses_a_malformed_authorization_code(secret_file):
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("GitHub must not be called with a malformed code")

    async with _client(handler) as client:
        with pytest.raises(GitHubOAuthError, match="malformed"):
            await exchange_code_for_token("no spaces allowed", client=client)


@pytest.mark.anyio
async def test_user_lookup_returns_the_identity(secret_file):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "id": 4242,
                "login": "octocat",
                "avatar_url": "https://avatars.example.com/octocat.png",
            },
        )

    async with _client(handler) as client:
        identity = await fetch_authenticated_user("gho_token", client=client)

    assert seen["url"] == "https://api.github.com/user"
    assert seen["authorization"] == "Bearer gho_token"
    assert identity.github_user_id == 4242
    assert identity.login == "octocat"


@pytest.mark.anyio
async def test_user_lookup_rejects_an_unusable_identity(secret_file):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 0, "login": ""})

    async with _client(handler) as client:
        with pytest.raises(GitHubOAuthError, match="unusable identity"):
            await fetch_authenticated_user("gho_token", client=client)
