"""`diffuse login` generates local secrets, links GitHub, and forwards agents."""

from __future__ import annotations

import argparse
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from diffuse.cli import agent as agent_cli
from diffuse.cli import github as github_cli
from diffuse.cli import login as login_cli


def _b64_decode_url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "==")


class _ConnectResponse:
    status_code = 200

    def json(self):
        return {
            "instance_id": "inst-1",
            "installation_id": 42,
            "instance_token": "token-secret",
            "delivery_signing_key": "signing-secret",
            "secrets_shown_once": True,
        }


def _login_args(**overrides):
    values = {
        "github": False,
        "agent": None,
        "code": None,
        "name": None,
        "url": "https://api.diffuse.website",
        "write_env": None,
        "print_secrets": False,
        "no_browser": False,
        "vendor_arguments": [],
        "login_parser": argparse.Namespace(print_help=lambda: None),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_generate_local_secrets_creates_all_keys_for_a_new_file(tmp_path):
    env = tmp_path / "deploy.env"
    generated = login_cli.generate_local_secrets(env)

    assert set(generated) == set(login_cli._LOCAL_SECRET_KEYS)
    text = env.read_text()
    for key in login_cli._LOCAL_SECRET_KEYS:
        assert f"{key}=" in text
    assert env.stat().st_mode & 0o777 == 0o600


def test_generate_local_secrets_is_idempotent(tmp_path):
    env = tmp_path / "deploy.env"
    first = login_cli.generate_local_secrets(env)
    before = env.read_text()

    second = login_cli.generate_local_secrets(env)

    assert second == {}
    assert env.read_text() == before
    assert first  # the first call actually generated something


def test_generate_local_secrets_preserves_existing_deployment_configuration(tmp_path):
    env = tmp_path / ".env"
    env.write_text("REVIEW_AGENT=codex\nKEEP=1\n")
    env.chmod(0o600)

    login_cli.generate_local_secrets(env)

    text = env.read_text()
    assert "REVIEW_AGENT=codex" in text
    assert "KEEP=1" in text
    for key in login_cli._LOCAL_SECRET_KEYS:
        assert f"{key}=" in text


def test_dispatch_public_key_derives_from_the_generated_private_key(tmp_path):
    env = tmp_path / "deploy.env"
    login_cli.generate_local_secrets(env)
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in env.read_text().splitlines()
        if "=" in line
    }
    private = _b64_decode_url(values["DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY"])
    public = _b64_decode_url(values["DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY"])

    derived = Ed25519PrivateKey.from_private_bytes(private).public_key()
    assert derived.public_bytes_raw() == public


def test_login_github_writes_local_and_integration_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *a, **k: _ConnectResponse())
    env = tmp_path / "deploy.env"

    login_cli._login(_login_args(code="c" * 40, write_env=str(env), no_browser=True))

    text = env.read_text()
    for key in login_cli._LOCAL_SECRET_KEYS:
        assert f"{key}=" in text
    assert "DIFFUSE_GITHUB_INTEGRATION_TOKEN=token-secret" in text
    assert "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY=signing-secret" in text
    assert env.stat().st_mode & 0o777 == 0o600
    # One-time secrets must never reach stdout.
    assert "token-secret" not in capsys.readouterr().out


def test_login_github_is_idempotent_across_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *a, **k: _ConnectResponse())
    env = tmp_path / "deploy.env"
    login_cli._login(_login_args(code="c" * 40, write_env=str(env), no_browser=True))
    before = env.read_text()

    login_cli._login(_login_args(code="c" * 40, write_env=str(env), no_browser=True))

    assert env.read_text() == before


def test_login_agent_forwards_to_agent_login(monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    def fake_login(namespace):
        calls.append((namespace.cli, list(namespace.vendor_arguments)))

    monkeypatch.setattr(agent_cli, "_login", fake_login)
    login_cli._login(
        _login_args(agent="codex", vendor_arguments=["--", "--device-auth"])
    )
    assert calls == [("codex", ["--device-auth"])]


def test_login_agent_does_not_touch_github(monkeypatch):
    called: list[object] = []
    monkeypatch.setattr(github_cli, "_connect_entry", lambda ns: called.append(ns))
    # Never spawn the real vendor CLI in tests; the agent handler is mocked out.
    monkeypatch.setattr(agent_cli, "_login", lambda ns: None)
    login_cli._login(_login_args(agent="claude", vendor_arguments=["--console"]))
    assert called == []


def test_status_reports_github_and_agent(monkeypatch, capsys):
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_URL", "https://api.diffuse.website")
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_TOKEN", "token-secret")

    class _Ready:
        status_code = 200

        def json(self):
            return {
                "ready": True,
                "installation_id": 42,
                "instance_id": "inst-1",
                "status": "ready",
            }

    monkeypatch.setattr(github_cli.httpx, "get", lambda *a, **k: _Ready())
    monkeypatch.setattr(login_cli.agent_cli, "_status", lambda ns: print("agent: ok"))
    login_cli._status(argparse.Namespace(url=None))
    out = capsys.readouterr().out
    assert '"ready": true' in out
    assert "agent: ok" in out


def test_login_subcommand_parses_agent_positionarg():
    from diffuse.cli.review import _build_parser

    parser, _commands = _build_parser()
    args = parser.parse_args(["login", "codex", "--", "--device-auth"])
    assert args.agent == "codex"
    # argparse's REMAINDER consumes the `--` separator itself.
    assert args.vendor_arguments == ["--device-auth"]

    github_args = parser.parse_args(["login"])
    assert github_args.agent is None
