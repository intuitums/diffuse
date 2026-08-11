import argparse
from unittest.mock import MagicMock

import pytest

from service import runtime


@pytest.mark.parametrize(
    ("arguments", "target", "forwarded"),
    [
        ([], "_run_api", []),
        (["serve"], "_run_api", []),
        (["worker", "--once"], "_run_worker", ["--once"]),
        (["healthcheck"], "_run_healthcheck", []),
        (["agent-host"], "_run_agent_runner", []),
        (["agent-host-healthcheck"], "_run_agent_runner_healthcheck", []),
        (["context-service"], "_run_context_service", []),
        (["repository", "list"], "_run_cli", ["repository", "list"]),
    ],
)
def test_runtime_dispatches_commands(monkeypatch, arguments, target, forwarded):
    handlers = {
        name: MagicMock()
        for name in (
            "_run_api",
            "_run_worker",
            "_run_healthcheck",
            "_run_agent_runner",
            "_run_agent_runner_healthcheck",
            "_run_context_service",
            "_run_cli",
        )
    }
    for name, handler in handlers.items():
        monkeypatch.setattr(runtime, name, handler)

    runtime.main(arguments)

    handlers[target].assert_called_once_with(forwarded)
    for name, handler in handlers.items():
        if name != target:
            handler.assert_not_called()


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_runtime_rejects_invalid_bind_port(monkeypatch, value):
    monkeypatch.setenv("DIFFUSE_BIND_PORT", value)

    with pytest.raises(ValueError, match="between 1 and 65535"):
        runtime._bind_port()


def test_runtime_healthcheck_requires_success(monkeypatch):
    response = MagicMock()
    response.status = 503
    response.__enter__.return_value = response
    monkeypatch.setattr(runtime, "urlopen", MagicMock(return_value=response))

    with pytest.raises(RuntimeError, match="HTTP 503"):
        runtime._run_healthcheck([])


def test_runtime_turns_operational_error_into_clean_exit(monkeypatch, capsys):
    monkeypatch.setattr(
        runtime,
        "_run_api",
        MagicMock(side_effect=ValueError("invalid configuration")),
    )

    with pytest.raises(SystemExit) as raised:
        runtime.main(["serve"])

    # The message moved from the SystemExit payload to formatted stderr so that
    # the exit code can carry the CLI's meaning instead. Still no traceback.
    assert raised.value.code == 3
    assert "invalid configuration" in capsys.readouterr().err


def test_serve_reports_a_configuration_failure_as_exit_three(monkeypatch, capsys):
    """The packaged entrypoint must share the CLI's exit-code contract.

    `runtime.main` previously raised `SystemExit(str(error))`, which exits 1 --
    the code reserved for "findings were reported and --fail-on-findings was
    supplied". An operator's `serve` failing on a bad DATABASE_URL was therefore
    indistinguishable from a review that found something.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", "sup3rs3cretvalue")

    def explode(_arguments):
        raise RuntimeError(
            "could not connect to postgresql://diffuse:sup3rs3cretvalue@db:5432/diffuse"
        )

    monkeypatch.setattr(runtime, "_run_api", explode)

    with pytest.raises(SystemExit) as raised:
        runtime.main(["serve"])

    assert raised.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("diffuse: error: ")
    # The credential must not reach container logs.
    assert "sup3rs3cretvalue" not in err


def test_worker_reports_an_unreachable_database_without_a_traceback(monkeypatch, capsys):
    """psycopg2.Error is not an OSError, so serve/worker must catch it explicitly.

    Found by running the packaged entrypoint against a dead DATABASE_URL: the
    original handler listed only (OSError, RuntimeError, ValueError), so
    psycopg2.OperationalError escaped as a raw traceback with the full
    connection string -- password included -- into container logs.
    """
    import psycopg2

    def explode(_arguments):
        raise psycopg2.OperationalError(
            'connection to server at "db", port 5432 failed: Connection refused'
        )

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://diffuse:sup3rs3cretvalue@db:5432/diffuse",
    )
    monkeypatch.setattr(runtime, "_run_worker", explode)

    with pytest.raises(SystemExit) as raised:
        runtime.main(["worker", "--once"])

    assert raised.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("diffuse: error: ")
    assert "Traceback" not in err
    assert "sup3rs3cretvalue" not in err
    assert "***" in err


def test_an_unexpected_failure_is_exit_four_not_exit_one(monkeypatch, capsys):
    monkeypatch.setattr(
        runtime,
        "_run_api",
        MagicMock(side_effect=KeyError("unexpected")),
    )

    with pytest.raises(SystemExit) as raised:
        runtime.main(["serve"])

    assert raised.value.code == 4
    assert "Internal error: KeyError" in capsys.readouterr().err


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_run_worker",
        MagicMock(side_effect=KeyboardInterrupt()),
    )

    # A shutdown signal must propagate, not become an exit-4 "internal error".
    with pytest.raises(KeyboardInterrupt):
        runtime.main(["worker"])


def test_cli_cancellation_has_no_traceback(capsys):
    """Interactive setup cancellation must leave the terminal in a clean state."""
    from service.cli.review import run_handler

    def cancel(_args):
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as raised:
        run_handler(argparse.Namespace(handler=cancel))

    assert raised.value.code == 130
    assert capsys.readouterr().err == "Diffuse cancelled.\n"


def test_api_configures_logging_from_log_level(monkeypatch):
    """LOG_LEVEL was documented for the API but only ever applied to the worker.

    Nothing called basicConfig on this path, so Diffuse's own loggers fell
    through to logging.lastResort, which drops INFO and DEBUG entirely.
    """
    import logging

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    run = MagicMock()
    monkeypatch.setattr("uvicorn.run", run)
    root = logging.getLogger()
    original_level = root.level
    try:
        runtime._run_api([])
        # uvicorn's own access log, and Diffuse's loggers, both have to follow
        # LOG_LEVEL; neither of these was wired up.
        assert run.call_args.kwargs["log_level"] == "debug"
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(original_level)


def test_api_log_level_survives_a_preinstalled_root_handler(monkeypatch):
    """Regression: basicConfig does nothing when the root logger has handlers.

    Importing the app pulls in a dependency that installs a RichHandler, so
    configuring the level with basicConfig alone left it at INFO no matter what
    LOG_LEVEL said.
    """
    import logging

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr("uvicorn.run", MagicMock())
    root = logging.getLogger()
    original_level, original_handlers = root.level, list(root.handlers)
    root.addHandler(logging.NullHandler())
    try:
        runtime._run_api([])
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(original_level)
        root.handlers = original_handlers


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("WARN", "warning"), ("warning", "warning"), ("  info  ", "info"), ("NOTSET", "debug")],
)
def test_log_level_is_translated_into_uvicorn_vocabulary(monkeypatch, configured, expected):
    """uvicorn has no WARN alias and no NOTSET, and rejects what it does not know.

    A LOG_LEVEL that is perfectly valid for the worker would otherwise crash-loop
    the API container.
    """
    monkeypatch.setenv("LOG_LEVEL", configured)

    assert runtime._log_level()[1] == expected


def test_unknown_log_level_is_rejected_by_name(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "chatty")

    with pytest.raises(ValueError, match="LOG_LEVEL"):
        runtime._log_level()
