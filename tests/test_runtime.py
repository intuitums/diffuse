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
        (["repository", "list"], "_run_cli", ["repository", "list"]),
    ],
)
def test_runtime_dispatches_commands(monkeypatch, arguments, target, forwarded):
    handlers = {
        name: MagicMock()
        for name in ("_run_api", "_run_worker", "_run_healthcheck", "_run_cli")
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
