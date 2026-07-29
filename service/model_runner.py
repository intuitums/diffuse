"""Permissioned host service that owns CLI process execution."""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from service.model_execution import ModelExecutionError, ModelRateLimitedError
from service.model_runner_adapters import (
    ProductionCLIBackend,
    cli_environment,
    run_supervised_process,
)
from service.model_runner_protocol import (
    MAX_RUNNER_REQUEST_BYTES,
    MAX_RUNNER_RESPONSE_BYTES,
    RunnerCancelRequest,
    RunnerCancelSuccess,
    RunnerError,
    RunnerGenerateRequest,
    RunnerGenerateSuccess,
    RunnerHealthRequest,
    RunnerHealthSuccess,
)

LOGGER = logging.getLogger(__name__)
RUNNER_VERSION = "diffuse-model-runner/0.1"
__all__ = ["cli_environment", "run_supervised_process"]


class RunnerBackend(Protocol):
    supported_executors: tuple[str, ...]

    def generate(self, request: RunnerGenerateRequest) -> RunnerGenerateSuccess:
        """Execute one request without exposing credentials or raw stderr."""

    def cancel(self, request_id: str) -> bool:
        """Terminate one active process group if this backend owns it."""


class DisabledCLIBackend:
    supported_executors: tuple[str, ...] = ()

    def generate(self, request: RunnerGenerateRequest) -> RunnerGenerateSuccess:
        raise LookupError(f"{request.executor} adapter is not installed in this runner")

    def cancel(self, request_id: str) -> bool:
        return False


class FakeCLIBackend:
    """Deterministic fault-injection backend enabled only by an explicit test flag."""

    supported_executors = ("codex-cli", "claude-cli")

    def generate(self, request: RunnerGenerateRequest) -> RunnerGenerateSuccess:
        prefix = "FAKE_RESPONSE:"
        if not request.user_prompt.startswith(prefix):
            raise ValueError("Fake runner requires a FAKE_RESPONSE payload")
        value = json.loads(request.user_prompt.removeprefix(prefix))
        if not isinstance(value, dict):
            raise ValueError("Fake runner response must be a JSON object")
        return RunnerGenerateSuccess(
            request_id=request.request_id,
            value=value,
            resolved_model=request.model,
            executor_version="fake-cli/1",
            finish_reason="stop",
        )

    def cancel(self, request_id: str) -> bool:
        return False


class _RunnerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        socket_path: str,
        backend: RunnerBackend,
        *,
        capacity: int,
    ) -> None:
        self.backend = backend
        self.capacity = capacity
        self.semaphore = threading.BoundedSemaphore(capacity)
        super().__init__(socket_path, _RunnerRequestHandler)


class _RunnerRequestHandler(socketserver.StreamRequestHandler):
    server: _RunnerServer

    def handle(self) -> None:
        line = self.rfile.readline(MAX_RUNNER_REQUEST_BYTES + 1)
        if not line or len(line) > MAX_RUNNER_REQUEST_BYTES or not line.endswith(b"\n"):
            self._error("unknown", "runner_protocol_error", False, "Invalid runner request")
            return
        try:
            payload = json.loads(line)
            operation = payload.get("operation")
            if operation == "health":
                request = RunnerHealthRequest.model_validate(payload)
                self._write(
                    RunnerHealthSuccess(
                        request_id=request.request_id,
                        runner_version=RUNNER_VERSION,
                        supported_executors=list(self.server.backend.supported_executors),
                        capacity=self.server.capacity,
                    )
                )
                return
            if operation == "cancel":
                request = RunnerCancelRequest.model_validate(payload)
                self._write(
                    RunnerCancelSuccess(
                        request_id=request.request_id,
                        cancelled=self.server.backend.cancel(request.target_request_id),
                    )
                )
                return
            request = RunnerGenerateRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, AttributeError):
            self._error("unknown", "runner_protocol_error", False, "Invalid runner request")
            return
        if not self.server.semaphore.acquire(blocking=False):
            self._error(request.request_id, "runner_busy", True, "Model runner is busy")
            return
        try:
            result = self.server.backend.generate(request)
            self._write(result)
        except TimeoutError:
            self._error(
                request.request_id,
                "model_request_timeout",
                True,
                "CLI generation exceeded its deadline",
            )
        except InterruptedError:
            self._error(
                request.request_id,
                "model_request_cancelled",
                False,
                "CLI generation was cancelled by Diffuse",
            )
        except LookupError:
            self._error(
                request.request_id,
                "executor_unavailable",
                False,
                "Requested CLI executor is not installed",
            )
        except ModelExecutionError as error:
            retry_after = (
                error.retry_after_seconds
                if isinstance(error, ModelRateLimitedError)
                else None
            )
            self._error(
                request.request_id,
                error.code,
                error.retryable,
                str(error),
                retry_after_seconds=retry_after,
            )
        except Exception as error:
            # Backend exceptions may wrap raw CLI stderr. Log only the coarse
            # type and request identity; adapters emit separately redacted
            # diagnostics when needed.
            LOGGER.error(
                "Model runner request failed request_id=%s error_type=%s",
                request.request_id,
                type(error).__name__,
            )
            self._error(
                request.request_id,
                "runner_internal_error",
                True,
                "CLI generation failed inside the local runner",
            )
        finally:
            self.server.semaphore.release()

    def _error(
        self,
        request_id: str,
        code: str,
        retryable: bool,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self._write(
            RunnerError(
                request_id=request_id,
                error_code=code,
                retryable=retryable,
                message=message,
                retry_after_seconds=retry_after_seconds,
            )
        )

    def _write(self, value) -> None:
        payload = value.model_dump_json().encode()
        if len(payload) > MAX_RUNNER_RESPONSE_BYTES:
            raise ValueError("Runner response exceeded its limit")
        self.wfile.write(payload + b"\n")


def serve(
    socket_path: Path,
    *,
    backend: RunnerBackend | None = None,
    capacity: int = 2,
    socket_group: str | None = None,
    ready: Callable[[], None] | None = None,
) -> None:
    if not socket_path.is_absolute():
        raise ValueError("Model runner socket path must be absolute")
    if not 1 <= capacity <= 128:
        raise ValueError("Model runner capacity must be between 1 and 128")
    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise ValueError("Model runner socket target exists and is not a socket")
        socket_path.unlink()
    try:
        with _RunnerServer(
            str(socket_path),
            backend or DisabledCLIBackend(),
            capacity=capacity,
        ) as server:
            socket_mode = 0o600
            if socket_group:
                try:
                    group_id = (
                        int(socket_group)
                        if socket_group.isdecimal()
                        else grp.getgrnam(socket_group).gr_gid
                    )
                except (KeyError, ValueError) as error:
                    raise ValueError("Model runner socket group does not exist") from error
                os.chown(socket_path, -1, group_id)
                socket_mode = 0o660
            os.chmod(socket_path, socket_mode)
            if ready is not None:
                ready()
            server.serve_forever(poll_interval=0.2)
    finally:
        if socket_path.exists() and socket_path.is_socket():
            socket_path.unlink()


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--socket",
        type=Path,
        required=True,
        help="Absolute Unix-socket path exposed to the Diffuse worker",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=2,
        help="Maximum concurrent CLI requests (default: 2)",
    )
    parser.add_argument(
        "--socket-group",
        default=os.environ.get("DIFFUSE_MODEL_RUNNER_GROUP"),
        help=(
            "Group name or GID allowed to connect; changes the socket from 0600 "
            "to 0660"
        ),
    )
    parser.add_argument(
        "--codex-executable",
        default=os.environ.get("DIFFUSE_CODEX_EXECUTABLE", "codex"),
        help="Runner-owned Codex executable name or absolute path",
    )
    parser.add_argument(
        "--claude-executable",
        default=os.environ.get("DIFFUSE_CLAUDE_EXECUTABLE", "claude"),
        help="Runner-owned Claude executable name or absolute path",
    )
    parser.add_argument(
        "--test-fake-backend",
        action="store_true",
        help="Use a deterministic fake backend; never use this for production",
    )
    parser.set_defaults(handler=_run)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Diffuse CLI model bridge")
    configure_parser(parser)
    return parser


def _run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO)
    backend = (
        FakeCLIBackend()
        if args.test_fake_backend
        else ProductionCLIBackend(
            codex_executable=args.codex_executable,
            claude_executable=args.claude_executable,
        )
    )
    serve(
        args.socket,
        backend=backend,
        capacity=args.capacity,
        socket_group=args.socket_group,
    )


def main() -> None:
    _run(_parser().parse_args())


if __name__ == "__main__":
    main()
