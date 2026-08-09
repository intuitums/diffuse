"""GitHub connect prefers writing one-time secrets to a mode-0600 env file."""

from __future__ import annotations

import argparse
import json

from service.cli import github as github_cli


class _Response:
    status_code = 200

    def json(self):
        return {
            "instance_id": "inst-1",
            "installation_id": 42,
            "instance_token": "token-secret",
            "event_signing_key": "signing-secret",
            "secrets_shown_once": True,
        }


def test_connect_write_env_keeps_secrets_out_of_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response())
    path = tmp_path / "github.env"
    github_cli._connect(
        argparse.Namespace(
            code="c" * 40,
            name="prod",
            url="https://api.diffuse.website",
            write_env=str(path),
        )
    )
    printed = capsys.readouterr()
    assert "token-secret" not in printed.out
    assert "signing-secret" not in printed.out
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert "DIFFUSE_GITHUB_INTEGRATION_TOKEN=token-secret" in text
    assert "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY=signing-secret" in text


def test_connect_stdout_warns_about_one_time_secrets(monkeypatch, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response())
    github_cli._connect(
        argparse.Namespace(
            code="c" * 40,
            name="prod",
            url="https://api.diffuse.website",
            write_env=None,
        )
    )
    printed = capsys.readouterr()
    assert "Prefer --write-env" in printed.err
    payload = json.loads(printed.out)
    assert payload["DIFFUSE_GITHUB_INTEGRATION_TOKEN"] == "token-secret"
