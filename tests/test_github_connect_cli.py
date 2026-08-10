"""GitHub connect prefers writing one-time secrets to a mode-0600 env file."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from service.cli import github as github_cli


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _ready_payload():
    return {
        "status": "ready",
        "instance_id": "inst-1",
        "installation_id": 42,
        "instance_token": "token-secret",
        "event_signing_key": "signing-secret",
        "secrets_shown_once": True,
    }


def test_connect_write_env_keeps_secrets_out_of_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response(_ready_payload()))
    path = tmp_path / "github.env"
    github_cli._connect(
        argparse.Namespace(
            code="c" * 40,
            name="prod",
            url="https://api.diffuse.website",
            write_env=str(path),
            no_browser=True,
            poll_seconds=0.01,
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
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response(_ready_payload()))
    github_cli._connect(
        argparse.Namespace(
            code="c" * 40,
            name="prod",
            url="https://api.diffuse.website",
            write_env=None,
            no_browser=True,
            poll_seconds=0.01,
        )
    )
    printed = capsys.readouterr()
    assert "Prefer --write-env" in printed.err
    payload = json.loads(printed.out)
    assert payload["DIFFUSE_GITHUB_INTEGRATION_TOKEN"] == "token-secret"


def test_connect_browser_session_polls_until_ready(monkeypatch, tmp_path, capsys):
    posts: list[str] = []
    gets: list[str] = []
    opened: list[str] = []

    def fake_post(url, **kwargs):
        posts.append(url)
        assert kwargs["json"] == {"display_name": "prod"}
        return _Response(
            {
                "session_id": "sess-1",
                "poll_token": "poll-secret",
                "browser_url": "https://api.diffuse.website/connect/sess-1",
                "expires_in_seconds": 900,
            }
        )

    poll_payloads = [{"status": "pending"}, _ready_payload()]

    def fake_get(url, **kwargs):
        gets.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer poll-secret"
        return _Response(poll_payloads.pop(0))

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    monkeypatch.setattr(github_cli.httpx, "get", fake_get)
    monkeypatch.setattr(github_cli.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(github_cli.time, "sleep", lambda _seconds: None)

    path = tmp_path / "github.env"
    github_cli._connect(
        argparse.Namespace(
            code=None,
            name="prod",
            url="https://api.diffuse.website",
            write_env=str(path),
            no_browser=False,
            poll_seconds=0.01,
        )
    )

    assert posts == ["https://api.diffuse.website/v1/connect/sessions"]
    assert gets == [
        "https://api.diffuse.website/v1/connect/sessions/sess-1",
        "https://api.diffuse.website/v1/connect/sessions/sess-1",
    ]
    assert opened == ["https://api.diffuse.website/connect/sess-1"]
    assert "token-secret" in path.read_text()
    assert "token-secret" not in capsys.readouterr().out


def test_connect_write_env_validates_path_before_register(monkeypatch, tmp_path):
    calls: list[object] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(_ready_payload())

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")
    with pytest.raises(ValueError, match="not writable"):
        github_cli._connect(
            argparse.Namespace(
                code="c" * 40,
                name="prod",
                url="https://api.diffuse.website",
                write_env=str(blocker / "github.env"),
                no_browser=True,
                poll_seconds=0.01,
            )
        )
    assert calls == []


def test_connect_write_env_rejects_a_symlink_before_register(monkeypatch, tmp_path):
    calls: list[object] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(_ready_payload())

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    target = tmp_path / "attacker.env"
    target.write_text("ATTACKER=1\n")
    path = tmp_path / "github.env"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="not writable"):
        github_cli._connect(
            argparse.Namespace(
                code="c" * 40,
                name="prod",
                url="https://api.diffuse.website",
                write_env=str(path),
                no_browser=True,
                poll_seconds=0.01,
            )
        )

    assert calls == []
    assert target.read_text() == "ATTACKER=1\n"


def test_preopened_write_env_descriptor_cannot_be_redirected_by_a_path_swap(tmp_path):
    path = tmp_path / "github.env"
    prepared_path, fd = github_cli._prepare_write_env_path(path)
    target = tmp_path / "attacker.env"
    target.write_text("ATTACKER=1\n")
    prepared_path.unlink()
    prepared_path.symlink_to(target)

    github_cli._write_env_file(
        fd,
        {
            "DIFFUSE_GITHUB_INTEGRATION_URL": "https://api.diffuse.website",
            "DIFFUSE_GITHUB_INTEGRATION_TOKEN": "token-secret",
            "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY": "signing-secret",
        },
    )

    assert target.read_text() == "ATTACKER=1\n"


def test_connect_write_env_locks_existing_permissive_file_before_secrets(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "github.env"
    path.write_text("OLD=1\n")
    path.chmod(0o666)
    assert path.stat().st_mode & 0o777 == 0o666

    modes_during_write: list[int] = []
    real_fdopen = os.fdopen

    def tracking_fdopen(fd, *args, **kwargs):
        modes_during_write.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response(_ready_payload()))
    monkeypatch.setattr(github_cli.os, "fdopen", tracking_fdopen)

    github_cli._connect(
        argparse.Namespace(
            code="c" * 40,
            name="prod",
            url="https://api.diffuse.website",
            write_env=str(path),
            no_browser=True,
            poll_seconds=0.01,
        )
    )
    assert modes_during_write
    assert all(mode == 0o600 for mode in modes_during_write)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token-secret" in path.read_text()
    assert "token-secret" not in capsys.readouterr().out
