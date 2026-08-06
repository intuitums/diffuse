"""The review compartment must be asserted at runtime, not just described in Compose."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from service.review import agent_compartment
from service.review.agent_environment import CREDENTIAL_ENVIRONMENT


class _Connection:
    def __init__(self, response: bytes = b"") -> None:
        self.response = response
        self.sent = b""

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        return self.response


def _configure_passing_preflight(monkeypatch, tmp_path: Path) -> _Connection:
    home = tmp_path / "agent-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setattr(agent_compartment, "COMPARTMENT_UID", os.geteuid())
    monkeypatch.setattr(agent_compartment, "COMPARTMENT_HOME", home)
    monkeypatch.setattr(agent_compartment, "REAL_HOME", Path("/home/diffuse"))
    monkeypatch.setenv("DIFFUSE_AGENT_HOME", str(home))
    monkeypatch.setenv("HTTP_PROXY", agent_compartment.PROXY_URL)
    monkeypatch.setenv("HTTPS_PROXY", agent_compartment.PROXY_URL)
    monkeypatch.setenv("NO_PROXY", "app,egress-proxy")
    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    vfs = type("Vfs", (), {"f_flag": 1})()
    monkeypatch.setattr(agent_compartment.os, "statvfs", lambda _path: vfs)
    proxy = _Connection(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    def connect(host: str, _port: int) -> _Connection:
        if host == agent_compartment.EGRESS_PROXY_HOST:
            return proxy
        raise OSError("unreachable from the compartment")

    monkeypatch.setattr(agent_compartment, "_connect", connect)
    return proxy


def test_preflight_asserts_the_runtime_compartment(monkeypatch, tmp_path):
    proxy = _configure_passing_preflight(monkeypatch, tmp_path)

    agent_compartment.preflight()

    assert proxy.sent.startswith(b"CONNECT api.anthropic.com:443 HTTP/1.1")


def test_preflight_names_the_first_failed_control(monkeypatch, tmp_path):
    _configure_passing_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_compartment.os, "geteuid", lambda: 0)

    with pytest.raises(agent_compartment.AgentCompartmentError, match="non-root pinned uid"):
        agent_compartment.preflight()


def test_credential_environment_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-reach-agent")

    with pytest.raises(agent_compartment.AgentCompartmentError, match="DATABASE_URL"):
        agent_compartment._check_no_credentials()


def test_direct_model_egress_is_refused(monkeypatch):
    monkeypatch.setattr(agent_compartment, "_connect", lambda *_args: _Connection())

    with pytest.raises(agent_compartment.AgentCompartmentError, match="direct egress"):
        agent_compartment._check_direct_egress_is_denied()


def test_agent_home_must_be_private_and_owned(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    monkeypatch.setattr(agent_compartment, "COMPARTMENT_HOME", home)
    monkeypatch.setenv("DIFFUSE_AGENT_HOME", str(home))

    with pytest.raises(agent_compartment.AgentCompartmentError, match="mode 0700"):
        agent_compartment._check_agent_home()


def test_connect_proxy_refuses_every_authority_except_the_vendor():
    server, client = socket.socketpair()
    thread = threading.Thread(target=agent_compartment._handle_proxy_connection, args=(server,))
    thread.start()
    client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    response = client.recv(256)
    client.close()
    thread.join(timeout=1)

    assert response.startswith(b"HTTP/1.1 403")
    assert not thread.is_alive()


def test_compose_keeps_agent_out_of_worker_environment_and_database_network():
    for path in (Path("docker-compose.yml"), Path("deploy/compose.yaml")):
        text = path.read_text()
        service = text.split("\n  agent-preflight:\n", 1)[1].split("\n  egress-proxy:", 1)[0]

        assert "env_file:" not in service
        assert "networks: [agent_mcp, agent_egress]" in service
        assert "DATABASE_URL" not in service
        assert "mem_limit:" in service
        assert "cpus:" in service
        assert "pids_limit:" in service
        assert "image: ${DIFFUSE_IMAGE" in text
        assert "backend:\n    internal: true" in text
        assert "agent_egress:\n    internal: true" in text
        assert "proxy_external:\n    internal: false" in text
        worker = text.split("\n  worker:\n", 1)[1].split("\n  agent-preflight:\n", 1)[0]
        assert "agent-preflight:\n        condition: service_completed_successfully" in worker


def test_compartment_session_bound_is_explicit_and_positive():
    assert agent_compartment.MAX_AGENT_SESSION_SECONDS == 600
