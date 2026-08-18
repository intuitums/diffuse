"""The review compartment must be asserted at runtime, not just described in Compose."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest
from diffuse_host import sandbox as agent_sandbox
from diffuse_host.environment import CREDENTIAL_ENVIRONMENT

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (
    REPOSITORY_ROOT / "compose.yaml",
    REPOSITORY_ROOT / "deploy" / "compose.yaml",
)


def _service_block(text: str, name: str) -> str:
    """Slice one Compose service out by its own indentation."""

    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        body.append(line)
    return "\n".join(body)


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


#: A stand-in for the compartment's pinned uid. Deliberately not `os.geteuid()`:
#: the container suite runs as root, and pinning the expected uid to whoever
#: happens to run the tests made a passing preflight impossible there --
#: `_check_identity` refuses euid 0 before it ever compares to the pin, so this
#: fixture asserted a state it had just made unreachable. The uid the process
#: reports and the uid owning the home are both faked, so the test describes the
#: compartment rather than the machine it runs on.
NON_ROOT_UID = 10001


def _configure_passing_preflight(monkeypatch, tmp_path: Path) -> _Connection:
    home = tmp_path / "agent-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setattr(agent_sandbox.os, "geteuid", lambda: NON_ROOT_UID)
    real_lstat = Path.lstat

    def lstat_owned_by_the_compartment(self: Path):
        metadata = real_lstat(self)
        if self != home:
            return metadata
        fields = list(metadata)
        fields[4] = NON_ROOT_UID  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", lstat_owned_by_the_compartment)
    monkeypatch.setattr(agent_sandbox, "COMPARTMENT_UID", NON_ROOT_UID)
    monkeypatch.setattr(agent_sandbox, "COMPARTMENT_HOME", home)
    monkeypatch.setattr(agent_sandbox, "REAL_HOME", Path("/home/diffuse"))
    monkeypatch.setenv("DIFFUSE_REVIEW_AGENT_HOME", str(home))
    monkeypatch.setenv("HTTP_PROXY", agent_sandbox.PROXY_URL)
    monkeypatch.setenv("HTTPS_PROXY", agent_sandbox.PROXY_URL)
    monkeypatch.setenv("NO_PROXY", "context-service,egress-proxy")
    for name in CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    vfs = type("Vfs", (), {"f_flag": 1})()
    monkeypatch.setattr(agent_sandbox.os, "statvfs", lambda _path: vfs)
    proxy = _Connection(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    def connect(host: str, _port: int) -> _Connection:
        if host == agent_sandbox.EGRESS_PROXY_HOST:
            return proxy
        raise OSError("unreachable from the compartment")

    monkeypatch.setattr(agent_sandbox, "_connect", connect)
    return proxy


def test_preflight_passes_even_when_the_suite_itself_runs_as_root(monkeypatch, tmp_path):
    """The container job runs the suite as root, and CI is where that showed up.

    Pinning the expected uid to `os.geteuid()` made a passing preflight
    impossible there: `_check_identity` refuses euid 0 outright, before it ever
    compares against the pin. The fixture describes a compartment, so it has to
    override the ambient identity rather than adopt it.
    """

    monkeypatch.setattr(agent_sandbox.os, "geteuid", lambda: 0)
    proxy = _configure_passing_preflight(monkeypatch, tmp_path)

    agent_sandbox.preflight()

    assert proxy.sent.startswith(b"CONNECT ")


def test_preflight_asserts_the_runtime_compartment(monkeypatch, tmp_path):
    proxy = _configure_passing_preflight(monkeypatch, tmp_path)

    agent_sandbox.preflight()

    assert proxy.sent.startswith(b"CONNECT api.anthropic.com:443 HTTP/1.1")


def test_preflight_names_the_first_failed_control(monkeypatch, tmp_path):
    _configure_passing_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_sandbox.os, "geteuid", lambda: 0)

    with pytest.raises(agent_sandbox.AgentCompartmentError, match="non-root pinned uid"):
        agent_sandbox.preflight()


def test_credential_environment_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-reach-agent")

    with pytest.raises(agent_sandbox.AgentCompartmentError, match="DATABASE_URL"):
        agent_sandbox._check_no_credentials()


def test_direct_model_egress_is_refused(monkeypatch):
    monkeypatch.setattr(agent_sandbox, "_connect", lambda *_args: _Connection())

    with pytest.raises(agent_sandbox.AgentCompartmentError, match="direct egress"):
        agent_sandbox._check_direct_egress_is_denied()


def test_agent_home_must_be_private_and_owned(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    monkeypatch.setattr(agent_sandbox, "COMPARTMENT_HOME", home)
    monkeypatch.setenv("DIFFUSE_REVIEW_AGENT_HOME", str(home))

    with pytest.raises(agent_sandbox.AgentCompartmentError, match="mode 0700"):
        agent_sandbox._check_agent_home()


def test_a_client_that_hangs_up_does_not_take_the_proxy_down():
    """The healthcheck does exactly this every two seconds.

    `_read_request` catches only the parse errors, not the `recv`, so a peer
    that closes early surfaced as an `OSError` in the accept loop's own frame
    and ended the process. Serving each client on its own thread with the errors
    contained is what keeps one bad client from being an outage.
    """

    server, client = socket.socketpair()
    client.close()

    agent_sandbox._serve_proxy_client(server)


def test_a_client_too_slow_to_send_its_request_is_dropped_not_fatal(monkeypatch):
    monkeypatch.setattr(agent_sandbox, "NETWORK_PROBE_TIMEOUT_SECONDS", 0.01)
    server, client = socket.socketpair()
    thread = threading.Thread(target=agent_sandbox._serve_proxy_client, args=(server,))
    thread.start()
    try:
        # Never sends a request line; the parse timeout must contain it.
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        client.close()


def test_the_proxy_serves_clients_concurrently(monkeypatch):
    """A CONNECT tunnel lives as long as its model call.

    Handling these in the accept loop meant one in-flight request blocked every
    other client, including the healthcheck that decides whether to restart the
    container.
    """

    released = threading.Event()
    upstreams: list[socket.socket] = []

    def slow_upstream(_host: str, _port: int) -> socket.socket:
        near, far = socket.socketpair()
        upstreams.extend((near, far))
        released.wait(timeout=5)
        return near

    monkeypatch.setattr(agent_sandbox, "_connect", slow_upstream)

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setattr(agent_sandbox, "EGRESS_PROXY_PORT", port)

    accepted: list[socket.socket] = []

    def serve_two():
        for _ in range(2):
            connection, _address = listener.accept()
            accepted.append(connection)
            threading.Thread(
                target=agent_sandbox._serve_proxy_client,
                args=(connection,),
                daemon=True,
            ).start()

    server_thread = threading.Thread(target=serve_two, daemon=True)
    server_thread.start()

    request = (
        f"CONNECT {agent_sandbox.MODEL_API_HOST}:443 HTTP/1.1\r\n"
        f"Host: {agent_sandbox.MODEL_API_HOST}:443\r\n\r\n"
    ).encode()
    clients = []
    try:
        for _ in range(2):
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            client.sendall(request)
            clients.append(client)
        # Both reached the handler while the first upstream connect is blocked.
        server_thread.join(timeout=5)
        assert len(accepted) == 2
    finally:
        released.set()
        for sock in (*clients, *upstreams):
            sock.close()
        listener.close()


def test_connect_proxy_refuses_every_authority_except_the_vendor():
    server, client = socket.socketpair()
    thread = threading.Thread(target=agent_sandbox._handle_proxy_connection, args=(server,))
    thread.start()
    client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    response = client.recv(256)
    client.close()
    thread.join(timeout=1)

    assert response.startswith(b"HTTP/1.1 403")
    assert not thread.is_alive()


def test_compose_keeps_agent_out_of_worker_environment_and_database_network():
    for path in COMPOSE_FILES:
        text = path.read_text()
        for runtime in ("claude", "codex"):
            service = _service_block(text, f"agent-host-{runtime}")
            assert "env_file:" not in service
            assert (
                f"networks: [agent_mcp_{runtime}, agent_egress_{runtime}, "
                f"runner_control_{runtime}]"
            ) in service
            assert "DATABASE_URL" not in service
            assert "mem_limit:" in service
            assert "cpus:" in service
            assert "pids_limit:" in service
        assert "image: ${DIFFUSE_IMAGE" in text
        assert "backend:\n    internal: true" in text
        for runtime in ("claude", "codex"):
            assert f"agent_mcp_{runtime}:\n    internal: true" in text
            assert f"agent_egress_{runtime}:\n    internal: true" in text
        assert "proxy_external:\n    internal: false" in text


def test_the_compartment_does_not_gate_the_review_pipeline():
    """Runner availability is a worker check, not a Compose startup dependency.

    As a `depends_on` of the worker this made a live CONNECT to the model API a
    precondition for starting reviews at all -- including for an operator on a
    different provider, who would have no way to satisfy it.
    """

    for path in COMPOSE_FILES:
        text = path.read_text()
        worker = text.split("\n  worker:\n", 1)[1].split("\n  agent-host-claude:\n", 1)[0]

        assert "depends_on:\n      agent-" not in worker
        for runtime in ("claude", "codex"):
            for service in (f"agent-host-{runtime}", f"egress-proxy-{runtime}"):
                assert f'profiles: ["agent-{runtime}"]' in _service_block(text, service)


def test_the_app_keeps_its_outbound_route():
    """`app` mints GitHub installation tokens and runs the OAuth code exchange.

    Every network it was given is `internal: true`, which leaves it able to
    reach the database and nothing else; both of those calls fail.
    """

    for path in COMPOSE_FILES:
        text = path.read_text()
        app = text.split("\n  app:\n", 1)[1].split("\n  worker:\n", 1)[0]
        networks = next(line for line in app.splitlines() if "networks:" in line)

        assert "control_egress" in networks
    assert "control_egress:\n    internal: false" in text


def test_the_allowlisted_model_host_follows_the_configured_provider(monkeypatch):
    """Diffuse does not require Anthropic, so the allowlist must not either."""

    for path in COMPOSE_FILES:
        assert "DIFFUSE_REVIEW_AGENT_MODEL_HOST" in path.read_text()
