"""Runtime checks for the container that will execute untrusted agent sessions.

This module deliberately has no review-runtime registration.  The compartment is
the boundary that makes a future hosted agent runtime possible, not a way to
turn one on before its adapter, credential lifecycle, and evaluation gates are
ready.  `preflight` is run as the compartment's own Compose service, where it
checks the kernel-visible properties that a YAML review cannot prove.

The egress proxy below is intentionally a small CONNECT-only relay.  It is not
a TLS inspection boundary: CONNECT exposes the requested authority, not the
subsequent encrypted HTTP requests.  Its job is to prevent accidental direct
egress from the compartment.  The no-shell/no-write tool policy remains the
control which prevents a determined agent process from repurposing the shared
network namespace.
"""

from __future__ import annotations

import os
import select
import socket
import stat
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from service.review.agent_environment import AGENT_HOME_VARIABLE, CREDENTIAL_ENVIRONMENT

# Unlike local agent hosting, the compartment has no reason to import the CLI
# host module (which in turn is allowed to inspect runtime state). Keep this
# path import-free so preflight itself performs no incidental provider work.
REAL_HOME = Path.home()

# This is a numeric Compose contract, not a value inherited from an operator's
# environment.  The credential-home lifecycle work provisions this uid's home;
# keeping the expected identity here lets that work and the preflight share one
# checkable boundary.
COMPARTMENT_UID = 10001
COMPARTMENT_HOME = Path("/var/lib/diffuse/agent")
DATABASE_HOST = "db"
DATABASE_PORT = 5432
EGRESS_PROXY_HOST = "egress-proxy"
EGRESS_PROXY_PORT = 3128
#: The one authority the compartment may reach. Configurable because Diffuse
#: does not require any particular provider -- `REVIEW_MODEL` and
#: `REVIEW_API_BASE` decide that -- and an allowlist hardcoded to one vendor
#: either blocks everyone else or gets switched off, which is worse.
MODEL_API_HOST = os.environ.get("DIFFUSE_AGENT_MODEL_HOST", "api.anthropic.com")
MODEL_API_PORT = 443
PROXY_URL = f"http://{EGRESS_PROXY_HOST}:{EGRESS_PROXY_PORT}"
NO_PROXY_VARIABLE = "NO_PROXY"
NETWORK_PROBE_TIMEOUT_SECONDS = 2.0
# The isolated agent-runner (Gate B/C) must use this upper bound when it drives
# a session.  The container
# limits below stop a process from monopolising the machine; this bound stops it
# from monopolising the operator's paid vendor seat.
MAX_AGENT_SESSION_SECONDS = 600


class AgentCompartmentError(ValueError):
    """The process is not running in the review compartment we require."""


@dataclass(frozen=True)
class CompartmentCheck:
    """One named runtime assertion, kept separately for actionable failures."""

    name: str
    verify: Callable[[], None]


def _expected_home() -> Path:
    value = os.environ.get(AGENT_HOME_VARIABLE)
    if value != str(COMPARTMENT_HOME):
        raise AgentCompartmentError(
            f"{AGENT_HOME_VARIABLE} must be {COMPARTMENT_HOME} in the review compartment"
        )
    return COMPARTMENT_HOME


def _check_identity() -> None:
    euid = os.geteuid()
    if euid == 0:
        raise AgentCompartmentError("review compartment must not run as root")
    if euid != COMPARTMENT_UID:
        raise AgentCompartmentError(
            f"review compartment euid must be pinned uid {COMPARTMENT_UID}, got {euid}"
        )


def _check_read_only_root() -> None:
    # POSIX defines ST_RDONLY as bit 1.  `statvfs` observes the mounted rootfs,
    # unlike trying to create a canary file, which is itself an unwanted write.
    if not os.statvfs("/").f_flag & 1:
        raise AgentCompartmentError("review compartment root filesystem must be read-only")


def _check_no_credentials() -> None:
    present = sorted(name for name in CREDENTIAL_ENVIRONMENT if name in os.environ)
    if present:
        raise AgentCompartmentError(
            "review compartment received credential environment variables: " + ", ".join(present)
        )


def _check_real_home() -> None:
    # `REAL_HOME` is captured before any child rewrites HOME.  A relative or
    # root home turns a deny/allow policy into a path that cannot mean what it
    # says, so refuse it before a session can run.
    if not REAL_HOME.is_absolute() or Path("/") == REAL_HOME:
        raise AgentCompartmentError(f"agent REAL_HOME is unsafe: {REAL_HOME}")


def _check_agent_home() -> None:
    home = _expected_home()
    try:
        metadata = home.lstat()
    except OSError as error:
        raise AgentCompartmentError(
            f"review compartment agent home is unavailable: {home}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentCompartmentError(
            f"review compartment agent home must be a real directory: {home}"
        )
    if metadata.st_uid != os.geteuid():
        raise AgentCompartmentError(
            f"review compartment agent home must be owned by euid {os.geteuid()}: {home}"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AgentCompartmentError(f"review compartment agent home must be mode 0700: {home}")


def _connect(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=NETWORK_PROBE_TIMEOUT_SECONDS)


def _check_database_is_unreachable() -> None:
    try:
        with _connect(DATABASE_HOST, DATABASE_PORT):
            pass
    except OSError:
        return
    raise AgentCompartmentError("review compartment can reach the database")


def _check_direct_egress_is_denied() -> None:
    try:
        with _connect(MODEL_API_HOST, MODEL_API_PORT):
            pass
    except OSError:
        return
    raise AgentCompartmentError(f"review compartment has direct egress to {MODEL_API_HOST}:443")


def _check_proxy_environment() -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY"):
        if os.environ.get(name) != PROXY_URL:
            raise AgentCompartmentError(f"{name} must point to the compartment egress proxy")
    # App traffic must not take the proxy, or the proxy becomes a bypass around
    # the MCP authorization boundary.  Compose names are explicit, not inferred
    # from an operator-provided URL.
    no_proxy = {
        part.strip()
        for part in os.environ.get(NO_PROXY_VARIABLE, "").split(",")
        if part.strip()
    }
    if not {"app", EGRESS_PROXY_HOST}.issubset(no_proxy):
        raise AgentCompartmentError("NO_PROXY must include app and egress-proxy")


def _check_proxy_connects() -> None:
    request = (
        f"CONNECT {MODEL_API_HOST}:{MODEL_API_PORT} HTTP/1.1\r\n"
        f"Host: {MODEL_API_HOST}:{MODEL_API_PORT}\r\n\r\n"
    ).encode("ascii")
    try:
        with _connect(EGRESS_PROXY_HOST, EGRESS_PROXY_PORT) as connection:
            connection.sendall(request)
            response = connection.recv(256)
    except OSError as error:
        raise AgentCompartmentError("review compartment cannot reach its egress proxy") from error
    if not response.startswith(b"HTTP/1.1 200"):
        raise AgentCompartmentError("review compartment egress proxy refused the model API CONNECT")


CHECKS: tuple[CompartmentCheck, ...] = (
    CompartmentCheck("non-root pinned uid", _check_identity),
    CompartmentCheck("read-only root filesystem", _check_read_only_root),
    CompartmentCheck("credential-free environment", _check_no_credentials),
    CompartmentCheck("sane real home", _check_real_home),
    CompartmentCheck("owned private agent home", _check_agent_home),
    CompartmentCheck("database isolation", _check_database_is_unreachable),
    CompartmentCheck("direct egress denial", _check_direct_egress_is_denied),
    CompartmentCheck("explicit proxy environment", _check_proxy_environment),
    CompartmentCheck("allowlisted proxy egress", _check_proxy_connects),
)


def preflight() -> None:
    """Fail closed with the first violated, named compartment property."""

    for check in CHECKS:
        try:
            check.verify()
        except AgentCompartmentError as error:
            raise AgentCompartmentError(
                f"review compartment preflight failed ({check.name}): {error}"
            ) from error


def run_preflight() -> None:
    """Entrypoint used by the one-shot Compose preflight service."""

    preflight()
    print("review compartment preflight passed", file=sys.stderr)


def _read_request(connection: socket.socket) -> tuple[str, int] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(1024)
        if not chunk:
            return None
        data += chunk
        if len(data) > 8192:
            return None
    try:
        request_line = data.split(b"\r\n", 1)[0].decode("ascii")
        method, authority, _version = request_line.split(" ", 2)
        host, separator, raw_port = authority.rpartition(":")
        if method != "CONNECT" or not separator:
            return None
        return host.lower().rstrip("."), int(raw_port)
    except (UnicodeDecodeError, ValueError):
        return None


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, (), ())
        for source in readable:
            target = right if source is left else left
            data = source.recv(65536)
            if not data:
                return
            target.sendall(data)


def _handle_proxy_connection(connection: socket.socket) -> None:
    request = _read_request(connection)
    if request != (MODEL_API_HOST, MODEL_API_PORT):
        connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return
    try:
        upstream = _connect(MODEL_API_HOST, MODEL_API_PORT)
    except OSError:
        # Only reachable before the tunnel is established, which is the only
        # point at which an HTTP response is still meaningful to the client.
        connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        return
    with upstream:
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # The short timeout protects parsing a new proxy request. A model
        # response can legitimately take minutes, so it must not become an
        # idle tunnel cutoff after CONNECT has been allowed.
        connection.settimeout(None)
        upstream.settimeout(None)
        # A relay failure is not reported in-band. `200 Connection Established`
        # has already been sent, so the client is speaking TLS and an
        # `HTTP/1.1 502` written into that stream is corruption, not an error
        # message. Dropping both ends is the only thing the client can read.
        with suppress(OSError):
            _relay(connection, upstream)


def _serve_proxy_client(connection: socket.socket) -> None:
    """Handle one client to completion, and never take the listener down.

    Every failure here belongs to one client: a peer that vanished mid-request,
    a slow sender that hit the parse timeout, a reset on the 403 write. All of
    them raise `OSError`, and this used to run in the accept loop's own frame,
    so any of them ended the process -- including the healthcheck, which
    connects and immediately closes every two seconds.
    """

    with connection, suppress(OSError):
        connection.settimeout(NETWORK_PROBE_TIMEOUT_SECONDS)
        _handle_proxy_connection(connection)


def run_egress_proxy() -> None:
    """Serve the compartment's fixed CONNECT allowlist until terminated.

    One thread per client. A CONNECT tunnel lives as long as the model call it
    carries, so serving these from the accept loop meant a single in-flight
    request blocked every other client -- and the healthcheck along with them.
    """

    with socket.create_server(("0.0.0.0", EGRESS_PROXY_PORT), reuse_port=False) as listener:
        while True:
            connection, _address = listener.accept()
            worker = threading.Thread(
                target=_serve_proxy_client,
                args=(connection,),
                daemon=True,
            )
            worker.start()
