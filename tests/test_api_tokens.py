import argparse
import string
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from service import review_cli, token_cli
from service.api_tokens import (
    MCP_READ_SCOPE,
    ServiceTokenRecord,
    api_token_sha256,
    create_service_token,
    generate_api_token,
    validate_api_token,
)


def test_service_token_validation_and_hashing_are_strict():
    credential = "z" * 48

    assert validate_api_token(credential) == credential
    assert api_token_sha256(credential) == api_token_sha256(credential)
    assert credential not in api_token_sha256(credential)

    for invalid in (
        "short",
        "x" * 31,
        "x" * 513,
        "x" * 40 + "\n",
        "x" * 40 + "\x7f",
        "é" * 40,
    ):
        with pytest.raises(ValueError, match="visible ASCII"):
            validate_api_token(invalid)


def test_service_token_rejects_bootstrap_secret_reuse(monkeypatch):
    credential = "b" * 48
    monkeypatch.setenv("DIFFUSE_API_TOKEN", credential)

    with pytest.raises(ValueError, match="must not reuse DIFFUSE_API_TOKEN"):
        create_service_token(
            None,
            name="looks-scoped",
            token=credential,
            scopes=(MCP_READ_SCOPE,),
            all_repositories=True,
            actor="operator",
        )


def test_token_metadata_output_never_contains_credential_material():
    now = datetime.now(UTC)
    record = ServiceTokenRecord(
        id=3,
        name="review-agent",
        scopes=(MCP_READ_SCOPE,),
        all_repositories=False,
        repository_ids=(7,),
        expires_at=None,
        created_by="operator",
        created_at=now,
        last_used_at=None,
        revoked_at=None,
        revoked_by=None,
        revocation_reason=None,
    )

    output = token_cli._record_json(record)

    assert output["id"] == 3
    assert output["repository_ids"] == [7]
    assert "token" not in output
    assert "hash" not in output


def test_unified_cli_routes_token_lifecycle_without_accepting_inline_secrets():
    parser = review_cli._parser()

    add = parser.parse_args(
        [
            "token",
            "add",
            "review-agent",
            "--token-env",
            "DIFFUSE_NEW_TOKEN",
            "--scope",
            MCP_READ_SCOPE,
            "--repository-id",
            "7",
            "--actor",
            "operator",
        ]
    )
    listing = parser.parse_args(["token", "list"])
    revoke = parser.parse_args(
        [
            "token",
            "revoke",
            "3",
            "--actor",
            "operator",
            "--reason",
            "rotation",
        ]
    )

    assert add.handler is token_cli._add_token
    assert add.repository_id == [7]
    assert listing.handler is token_cli._list_tokens
    assert revoke.handler is token_cli._revoke_token

    minting = parser.parse_args(
        [
            "token",
            "add",
            "minted-agent",
            "--scope",
            MCP_READ_SCOPE,
            "--all-repositories",
            "--actor",
            "operator",
        ]
    )

    assert minting.token_env is None
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "token",
                "add",
                "unsafe",
                "--token-value",
                "s3cret-on-the-command-line",
                "--scope",
                MCP_READ_SCOPE,
                "--all-repositories",
                "--actor",
                "operator",
            ]
        )


def test_minted_service_tokens_are_high_entropy_url_safe_and_unique():
    alphabet = set(string.ascii_letters + string.digits + "-_")

    minted = [generate_api_token() for _ in range(256)]

    assert len(set(minted)) == len(minted)
    for token in minted:
        assert validate_api_token(token) == token
        # secrets.token_urlsafe(32) is 256 bits of CSPRNG output, so an
        # unsalted digest of it stays out of wordlist range.
        assert len(token) >= 43
        assert set(token) <= alphabet
        assert len(set(token)) >= 16


def test_token_add_mints_a_credential_and_prints_it_exactly_once(monkeypatch, capsys):
    record = ServiceTokenRecord(
        id=11,
        name="minted-agent",
        scopes=(MCP_READ_SCOPE,),
        all_repositories=True,
        repository_ids=(),
        expires_at=None,
        created_by="operator",
        created_at=datetime.now(UTC),
        last_used_at=None,
        revoked_at=None,
        revoked_by=None,
        revocation_reason=None,
    )
    stored: list[str] = []

    def _create(_conn, **kwargs):
        stored.append(kwargs["token"])
        return record

    monkeypatch.setattr(token_cli, "get_conn", MagicMock)
    monkeypatch.setattr(token_cli, "create_service_token", _create)

    token_cli._add_token(
        argparse.Namespace(
            name="minted-agent",
            token_env=None,
            scope=[MCP_READ_SCOPE],
            repository_id=None,
            all_repositories=True,
            actor="operator",
            expires_in_days=None,
        )
    )

    output = capsys.readouterr().out
    assert len(stored) == 1
    credential = stored[0]
    assert validate_api_token(credential) == credential
    assert output.count(credential) == 1
    assert api_token_sha256(credential) not in output


def test_token_add_never_prints_an_operator_supplied_credential(monkeypatch, capsys):
    credential = "q" * 48
    monkeypatch.setenv("DIFFUSE_NEW_TOKEN", credential)
    monkeypatch.setattr(token_cli, "get_conn", MagicMock)
    monkeypatch.setattr(
        token_cli,
        "create_service_token",
        lambda _conn, **kwargs: ServiceTokenRecord(
            id=12,
            name=kwargs["name"],
            scopes=kwargs["scopes"],
            all_repositories=True,
            repository_ids=(),
            expires_at=None,
            created_by=kwargs["actor"],
            created_at=datetime.now(UTC),
            last_used_at=None,
            revoked_at=None,
            revoked_by=None,
            revocation_reason=None,
        ),
    )

    token_cli._add_token(
        argparse.Namespace(
            name="legacy-agent",
            token_env="DIFFUSE_NEW_TOKEN",
            scope=[MCP_READ_SCOPE],
            repository_id=None,
            all_repositories=True,
            actor="operator",
            expires_in_days=None,
        )
    )

    assert credential not in capsys.readouterr().out
