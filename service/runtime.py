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


def _run_api(arguments: Sequence[str]) -> None:
    if arguments:
        raise ValueError("The serve command does not accept positional arguments")
    import uvicorn

    from service.webhook_server import app

    uvicorn.run(
        app,
        host=os.environ.get("DIFFUSE_BIND_HOST", DEFAULT_BIND_HOST),
        port=_bind_port(),
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
            _run_cli([command, *selected])
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
