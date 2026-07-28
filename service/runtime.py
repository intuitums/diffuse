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

    from service.webhook_server import app

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
    from service.worker import main as worker_main

    _replace_process_arguments(arguments)
    worker_main()


def _run_healthcheck(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The healthcheck command does not accept positional arguments")
    url = os.environ.get("DIFFUSE_HEALTHCHECK_URL", DEFAULT_HEALTHCHECK_URL)
    with urlopen(url, timeout=_healthcheck_timeout()) as response:
        if response.status != 200:
            raise RuntimeError(f"Diffuse readiness check returned HTTP {response.status}")


def _run_cli(arguments: Sequence[str]) -> None:
    from service.review_cli import main as cli_main

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
        elif command == "healthcheck":
            _run_healthcheck(selected)
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

        from service.review_cli import (
            EXIT_CONFIG,
            EXIT_INTERNAL,
            database_error_message,
            format_cli_error,
        )

        if isinstance(error, (SystemExit, KeyboardInterrupt)):
            raise
        if isinstance(error, psycopg2.Error):
            sys.stderr.write(format_cli_error(database_error_message(error)))
            raise SystemExit(EXIT_CONFIG) from error
        if isinstance(error, (OSError, RuntimeError, ValueError)):
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
