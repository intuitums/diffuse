"""GitHub connect prefers writing one-time secrets to a mode-0600 env file."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from service.cli import github as github_cli


class _Response:
    status_code = 200

    def json(self):
        return {
            "instance_id": "inst-1",
            "installation_id": 42,
            "instance_token": "token-secret",
            "delivery_signing_key": "signing-secret",
            "secrets_shown_once": True,
        }


def _connect_args(**overrides):
    values = {
        "code": "c" * 40,
        "name": "prod",
        "url": "https://api.diffuse.website",
        "write_env": None,
        "print_secrets": False,
        "no_browser": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_connect_write_env_keeps_secrets_out_of_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response())
    path = tmp_path / "github.env"
    github_cli._connect(_connect_args(write_env=str(path)))
    printed = capsys.readouterr()
    assert "token-secret" not in printed.out
    assert "signing-secret" not in printed.out
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert "DIFFUSE_GITHUB_INTEGRATION_TOKEN=token-secret" in text
    assert "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY=signing-secret" in text


def test_connect_defaults_to_github_integration_env_and_hostname(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github_cli.socket, "gethostname", lambda: "reviewer-1.local")
    captured: list[dict[str, object]] = []

    def fake_post(url, **kwargs):
        captured.append(kwargs.get("json") or {})
        return _Response()

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    github_cli._connect(_connect_args(code="c" * 40, name=None, write_env=None))
    assert captured[0]["display_name"] == "reviewer-1.local"
    path = tmp_path / "github-integration.env"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token-secret" not in capsys.readouterr().out


def test_connect_print_secrets_warns_about_one_time_secrets(monkeypatch, capsys):
    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response())
    github_cli._connect(_connect_args(print_secrets=True))
    printed = capsys.readouterr()
    assert "github-integration.env" in printed.err
    payload = json.loads(printed.out)
    assert payload["DIFFUSE_GITHUB_INTEGRATION_TOKEN"] == "token-secret"


def test_connect_write_env_validates_path_before_register(monkeypatch, tmp_path):
    calls: list[object] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response()

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")
    with pytest.raises(ValueError, match="not writable"):
        github_cli._connect(_connect_args(write_env=str(blocker / "github.env")))
    assert calls == []


def test_connect_write_env_rejects_a_symlink_before_register(monkeypatch, tmp_path):
    calls: list[object] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response()

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    target = tmp_path / "attacker.env"
    target.write_text("ATTACKER=1\n")
    path = tmp_path / "github.env"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="not writable"):
        github_cli._connect(_connect_args(write_env=str(path)))

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

    monkeypatch.setattr(github_cli.httpx, "post", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(github_cli.os, "fdopen", tracking_fdopen)

    github_cli._connect(_connect_args(write_env=str(path)))
    assert modes_during_write
    assert all(mode == 0o600 for mode in modes_during_write)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token-secret" in path.read_text()
    assert "token-secret" not in capsys.readouterr().out


class _SessionCreateResponse:
    status_code = 200

    def json(self):
        return {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "poll_secret": "poll-secret",
            "browser_url": "https://api.diffuse.website/auth/github/connect/"
            "11111111-1111-1111-1111-111111111111",
            "expires_in_seconds": 900,
        }


class _SessionReadyResponse:
    status_code = 200

    def json(self):
        return {
            "status": "ready",
            "instance_id": "inst-1",
            "installation_id": 42,
            "instance_token": "token-secret",
            "delivery_signing_key": "signing-secret",
            "secrets_shown_once": True,
        }


def test_connect_browser_flow_polls_until_ready(monkeypatch, tmp_path, capsys):
    posts: list[object] = []
    gets: list[object] = []

    def fake_post(url, **kwargs):
        posts.append(url)
        return _SessionCreateResponse()

    def fake_get(url, **kwargs):
        gets.append(url)
        return _SessionReadyResponse()

    monkeypatch.setattr(github_cli.httpx, "post", fake_post)
    monkeypatch.setattr(github_cli.httpx, "get", fake_get)
    monkeypatch.setattr(github_cli.webbrowser, "open", lambda url: False)
    path = tmp_path / "github.env"
    github_cli._connect(_connect_args(code=None, write_env=str(path), no_browser=True))
    assert posts == ["https://api.diffuse.website/v1/connect/sessions"]
    assert gets == [
        "https://api.diffuse.website/v1/connect/sessions/"
        "11111111-1111-1111-1111-111111111111"
    ]
    printed = capsys.readouterr()
    assert "token-secret" not in printed.out
    assert "Open this URL" in printed.err
    assert path.read_text().count("token-secret") == 1


class _StatusResponse:
    status_code = 200

    def json(self):
        return {
            "ready": True,
            "status": "ready",
            "installation_id": 42,
            "instance_id": "inst-1",
            "display_name": "prod",
            "installation_active": True,
            "pending_events": 0,
        }


class _DisconnectResponse:
    status_code = 200

    def json(self):
        return {"status": "disconnected"}


def test_status_prints_ready_payload(monkeypatch, capsys):
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_URL", "https://api.diffuse.website")
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_TOKEN", "token-secret")
    monkeypatch.setattr(github_cli.httpx, "get", lambda *args, **kwargs: _StatusResponse())
    github_cli._status(argparse.Namespace(url=None))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["installation_id"] == 42


def test_status_exits_nonzero_when_not_ready(monkeypatch):
    class NotReady:
        status_code = 200

        def json(self):
            return {"ready": False, "status": "not_ready", "diagnostic": "suspended"}

    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_URL", "https://api.diffuse.website")
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_TOKEN", "token-secret")
    monkeypatch.setattr(github_cli.httpx, "get", lambda *args, **kwargs: NotReady())
    with pytest.raises(SystemExit) as error:
        github_cli._status(argparse.Namespace(url=None))
    assert error.value.code == 1


def test_disconnect_revokes_instance(monkeypatch, capsys):
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_URL", "https://api.diffuse.website")
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_TOKEN", "token-secret")
    monkeypatch.setattr(
        github_cli.httpx, "post", lambda *args, **kwargs: _DisconnectResponse()
    )
    github_cli._disconnect(argparse.Namespace(url=None))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disconnected"
    assert "Remove DIFFUSE_GITHUB_INTEGRATION_TOKEN" in payload["next"]
