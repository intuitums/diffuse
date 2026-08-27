"""Single executable entry point for packaged Diffuse runtimes."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from urllib.request import urlopen

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8000
DEFAULT_HEALTHCHECK_URL = "http://127.0.0.1:8000/ready"
DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS = 5.0


def _bind_port() -> int:
    value = int(os.environ.get("DIFFUSE_BIND_PORT", str(DEFAULT_BIND_PORT)))
    if not 1 <= value <= 65535:
        raise ValueError("DIFFUSE_BIND_PORT must be between 1 and 65535")
    return value


def _healthcheck_timeout() -> float:
    value = float(
        os.environ.get(
            "DIFFUSE_HEALTHCHECK_TIMEOUT_SECONDS",
            str(DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS),
        )
    )
    if value <= 0:
        raise ValueError("DIFFUSE_HEALTHCHECK_TIMEOUT_SECONDS must be positive")
    return value


def _replace_process_arguments(arguments: Sequence[str]) -> None:
    sys.argv = [sys.argv[0], *arguments]


# uvicorn accepts a narrower vocabulary than `logging` does: it has no NOTSET and
# no WARN alias, and rejects anything outside this set. Mapping here keeps a
# LOG_LEVEL that is valid for the worker from crash-looping the API container.
_UVICORN_LOG_LEVELS = {
    "CRITICAL": "critical",
    "FATAL": "critical",
    "ERROR": "error",
    "WARNING": "warning",
    "WARN": "warning",
    "INFO": "info",
    "DEBUG": "debug",
    "NOTSET": "debug",
}


def _log_level() -> tuple[str, str]:
    """Return the LOG_LEVEL as (logging name, uvicorn name)."""
    name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if name not in _UVICORN_LOG_LEVELS:
        raise ValueError(
            f"LOG_LEVEL must be one of {', '.join(sorted(_UVICORN_LOG_LEVELS))}; got {name!r}"
        )
    return name, _UVICORN_LOG_LEVELS[name]


def _run_api(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The serve command does not accept positional arguments")
    import logging

    import uvicorn

    from diffuse.api.app import app

    # Both env files describe LOG_LEVEL as applying to "the worker and API", but
    # only the worker configured logging. Diffuse's own loggers here fell through
    # to logging.lastResort, which emits WARNING and above and discards INFO and
    # DEBUG -- so every refused webhook delivery that deployment.md tells an
    # operator to look for was reaching stderr only because it is a warning, and
    # raising LOG_LEVEL to DEBUG did nothing at all.
    logging_level, uvicorn_level = _log_level()
    # `basicConfig` alone is not enough here. Importing the app pulls in a
    # dependency that installs a RichHandler on the root logger, and
    # `basicConfig` is documented to do nothing once the root logger has
    # handlers -- so it silently leaves the level at INFO. Setting the level
    # explicitly applies it whether or not something got there first.
    logging.basicConfig(level=logging_level)
    logging.getLogger().setLevel(logging_level)

    uvicorn.run(
        app,
        host=os.environ.get("DIFFUSE_BIND_HOST", DEFAULT_BIND_HOST),
        port=_bind_port(),
        log_level=uvicorn_level,
    )


def _run_worker(arguments: Sequence[str]) -> None:
    from diffuse.worker import main as worker_main

    _replace_process_arguments(arguments)
    worker_main()


def _run_github_delivery_poller(arguments: Sequence[str]) -> None:
    from diffuse.github.delivery_poller import main as poller_main

    _replace_process_arguments(arguments)
    poller_main()


def _run_healthcheck(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The healthcheck command does not accept positional arguments")
    url = os.environ.get("DIFFUSE_HEALTHCHECK_URL", DEFAULT_HEALTHCHECK_URL)
    with urlopen(url, timeout=_healthcheck_timeout()) as response:
        if response.status != 200:
            raise RuntimeError(f"Diffuse readiness check returned HTTP {response.status}")


def _run_review_compartment_preflight(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The review-compartment-preflight command does not accept arguments")
    from diffuse_host.sandbox import run_preflight

    run_preflight()


def _run_egress_proxy(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The egress-proxy command does not accept arguments")
    from diffuse_host.sandbox import run_egress_proxy

    run_egress_proxy()


def _run_egress_proxy_healthcheck(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The egress-proxy-healthcheck command does not accept arguments")
    import socket

    # A bare connect proves nothing: the kernel completes it from the listen
    # backlog whether or not anything is accepting. Send a request the proxy is
    # required to refuse and require the refusal, which exercises accept, parse,
    # and reply.
    with socket.create_connection(("127.0.0.1", 3128), timeout=2) as probe:
        probe.sendall(
            b"CONNECT healthcheck.invalid:443 HTTP/1.1\r\n"
            b"Host: healthcheck.invalid:443\r\n\r\n"
        )
        response = probe.recv(64)
    if not response.startswith(b"HTTP/1.1 403"):
        raise RuntimeError(
            f"Egress proxy did not refuse a disallowed authority: {response[:32]!r}"
        )


def _run_agent_runner(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The agent-host command does not accept positional arguments")
    import uvicorn
    from diffuse_host.api import app

    _logging_level, uvicorn_level = _log_level()
    uvicorn.run(app, host=DEFAULT_BIND_HOST, port=8010, log_level=uvicorn_level)


def _run_agent_runner_healthcheck(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError(
            "The agent-host-healthcheck command does not accept positional arguments"
        )
    import json

    with urlopen("http://127.0.0.1:8010/v1/status", timeout=_healthcheck_timeout()) as response:
        if response.status != 200:
            raise RuntimeError(f"Agent runner readiness returned HTTP {response.status}")
        try:
            state = json.loads(response.read()).get("state")
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError("Agent runner readiness response is invalid") from error
    if state != "ready":
        raise RuntimeError(f"Agent runner is not ready: {state or 'unknown'}")


def _run_context_service(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The context-service command does not accept positional arguments")
    import uvicorn

    from diffuse.investigation.context_service import app

    _logging_level, uvicorn_level = _log_level()
    uvicorn.run(app, host=DEFAULT_BIND_HOST, port=8011, log_level=uvicorn_level)


def _run_cli(arguments: Sequence[str]) -> None:
    from diffuse.cli.review import main as cli_main

    _replace_process_arguments(arguments)
    cli_main()


def main(arguments: Sequence[str] | None = None) -> None:
    selected = list(sys.argv[1:] if arguments is None else arguments)
    command = selected.pop(0) if selected else "serve"
    try:
        if command == "serve":
            _run_api(selected)
        elif command == "worker":
            _run_worker(selected)
        elif command == "github-delivery-poller":
            _run_github_delivery_poller(selected)
        elif command == "healthcheck":
            _run_healthcheck(selected)
        elif command == "review-compartment-preflight":
            _run_review_compartment_preflight(selected)
        elif command == "egress-proxy":
            _run_egress_proxy(selected)
        elif command == "egress-proxy-healthcheck":
            _run_egress_proxy_healthcheck(selected)
        elif command == "agent-host":
            _run_agent_runner(selected)
        elif command == "agent-host-healthcheck":
            _run_agent_runner_healthcheck(selected)
        elif command == "context-service":
            _run_context_service(selected)
        else:
            # The CLI owns its own exit codes and error formatting.
            _run_cli([command, *selected])
    except BaseException as error:
        # serve/worker/healthcheck share the CLI's exit-code contract, so a
        # configuration or environment failure must not exit 1 -- that code means
        # "findings were reported". Secrets are stripped too, because this text
        # lands in container logs and an unreachable DATABASE_URL otherwise
        # surfaces the whole connection string, password included.
        #
        # psycopg2.Error is handled explicitly: it descends from Exception, not
        # OSError, so an unreachable database on the serve/worker path used to
        # escape as an unformatted traceback with the credential in it.
        import psycopg2

        from diffuse.cli.review import (
            EXIT_CONFIG,
            EXIT_INTERNAL,
            database_error_message,
            format_cli_error,
        )

        if isinstance(error, SystemExit | KeyboardInterrupt):
            raise
        if isinstance(error, psycopg2.Error):
            sys.stderr.write(format_cli_error(database_error_message(error)))
            raise SystemExit(EXIT_CONFIG) from error
        if isinstance(error, OSError | RuntimeError | ValueError):
            sys.stderr.write(format_cli_error(str(error)))
            raise SystemExit(EXIT_CONFIG) from error
        sys.stderr.write(
            format_cli_error(
                f"Internal error: {error.__class__.__name__}: {error}"
            )
        )
        raise SystemExit(EXIT_INTERNAL) from error


if __name__ == "__main__":
    main()
