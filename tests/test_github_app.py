from __future__ import annotations

import logging
import stat

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from service.github import app as github_app

APP_ENVIRONMENT = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_FILE",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_app_authentication(monkeypatch):
    for name in APP_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    github_app.reset_installation_token_cache()
    yield
    github_app.reset_installation_token_cache()


@pytest.fixture
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def configure_app(monkeypatch, private_key: str, *, installation_id: str = "456") -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "Iv1.test-client-id")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", installation_id)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", private_key)


def test_static_token_is_only_the_unconfigured_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")

    assert github_app.github_token() == "fallback-token"


def test_partial_app_configuration_fails_closed_instead_of_using_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-used")

    with pytest.raises(
        github_app.GitHubAppConfigurationError,
        match="partially configured",
    ):
        github_app.github_token()


def test_inline_private_key_accepts_docker_escaped_newlines(monkeypatch, private_key):
    configure_app(monkeypatch, private_key.replace("\n", "\\n"))

    assert github_app.app_credentials() == github_app.AppCredentials(
        app_id="Iv1.test-client-id",
        installation_id="456",
        private_key=private_key.strip(),
    )


def test_file_private_key_wins_and_warns_when_world_readable(
    monkeypatch,
    tmp_path,
    private_key,
    caplog,
):
    key_file = tmp_path / "github-app.pem"
    key_file.write_text(private_key)
    key_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    configure_app(monkeypatch, "-----BEGIN PRIVATE KEY-----\\nwrong")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(key_file))

    with caplog.at_level(logging.WARNING):
        credentials = github_app.app_credentials()

    assert credentials is not None
    assert credentials.private_key == private_key
    assert "group- or world-readable" in caplog.text


def test_group_writable_private_key_file_is_rejected(monkeypatch, tmp_path, private_key):
    key_file = tmp_path / "github-app.pem"
    key_file.write_text(private_key)
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IWGRP)
    configure_app(monkeypatch, private_key)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(key_file))

    with pytest.raises(
        github_app.GitHubAppConfigurationError,
        match="group- or world-writable",
    ):
        github_app.app_credentials()


def test_app_jwt_uses_github_claims_and_rs256(monkeypatch, private_key):
    configure_app(monkeypatch, private_key)
    monkeypatch.setattr(github_app.time, "time", lambda: 2_000_000_000)
    credentials = github_app.app_credentials()

    encoded = github_app._mint_app_jwt(credentials)
    public_key = serialization.load_pem_private_key(
        private_key.encode(),
        password=None,
    ).public_key()
    payload = jwt.decode(
        encoded,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert payload == {
        "iat": 2_000_000_000 - github_app.JWT_BACKDATE_SECONDS,
        "exp": (
            2_000_000_000
            - github_app.JWT_BACKDATE_SECONDS
            + github_app.JWT_LIFETIME_SECONDS
        ),
        "iss": "Iv1.test-client-id",
    }


def test_worker_startup_rejects_a_private_key_that_cannot_sign(monkeypatch):
    from service.hosted import worker

    # REVIEW_MODEL has no default and is probed before this one, so without a
    # valid value the refusal would name REVIEW_MODEL rather than the App.
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    configure_app(
        monkeypatch,
        "-----BEGIN PRIVATE KEY-----\nnot-a-real-rsa-key\n-----END PRIVATE KEY-----",
    )

    with pytest.raises(ValueError, match="GitHub App authentication is invalid"):
        worker.validate_worker_configuration()


def test_installation_token_is_cached_and_sent_with_the_api_version(
    monkeypatch,
    private_key,
):
    configure_app(monkeypatch, private_key)
    monkeypatch.setenv("GITHUB_API_VERSION", "2026-03-10")
    monkeypatch.setattr(github_app, "_mint_app_jwt", lambda _credentials: "app-jwt")
    monkeypatch.setattr(github_app.time, "monotonic", lambda: 100.0)
    requests: list[tuple[str, dict[str, str]]] = []

    def post(url, *, headers, timeout):
        assert timeout > 0
        requests.append((url, headers))
        return httpx.Response(
            httpx.codes.CREATED,
            json={"token": "installation-token", "expires_at": "ignored"},
        )

    monkeypatch.setattr(github_app.httpx, "post", post)

    assert github_app.github_token() == "installation-token"
    assert github_app.github_token() == "installation-token"
    assert requests == [
        (
            "https://api.github.com/app/installations/456/access_tokens",
            {
                "Authorization": "Bearer app-jwt",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "diffuse-github-app",
            },
        )
    ]


def test_installation_token_refreshes_before_expiry(monkeypatch, private_key):
    configure_app(monkeypatch, private_key)
    now = [100.0]
    issued = iter(("token-1", "token-2"))
    monkeypatch.setattr(github_app.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        github_app,
        "_exchange_for_installation_token",
        lambda _credentials: (next(issued), now[0] + 3600),
    )

    assert github_app.github_token() == "token-1"
    now[0] = 3_399.0
    assert github_app.github_token() == "token-1"
    now[0] = 3_400.0
    assert github_app.github_token() == "token-2"


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (httpx.codes.UNAUTHORIZED, "rejected the App JWT"),
        (httpx.codes.NOT_FOUND, "no installation 456"),
        (httpx.codes.FORBIDDEN, "HTTP 403"),
    ],
)
def test_token_exchange_errors_do_not_include_response_bodies(
    monkeypatch,
    private_key,
    status_code,
    message,
):
    configure_app(monkeypatch, private_key)
    monkeypatch.setattr(github_app, "_mint_app_jwt", lambda _credentials: "app-jwt")
    monkeypatch.setattr(
        github_app.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            status_code,
            text="sensitive upstream diagnostic",
        ),
    )

    with pytest.raises(github_app.GitHubAppError, match=message) as raised:
        github_app.github_token()

    assert "sensitive upstream diagnostic" not in str(raised.value)


def test_worker_configuration_names_partial_app_authentication(monkeypatch):
    from service.hosted import worker

    # See the note above: REVIEW_MODEL is probed first and has no default.
    monkeypatch.setenv("REVIEW_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("GITHUB_APP_ID", "123")

    with pytest.raises(ValueError, match="GitHub App authentication is invalid"):
        worker.validate_worker_configuration()
