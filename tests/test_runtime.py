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


def test_runtime_turns_operational_error_into_clean_exit(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_run_api",
        MagicMock(side_effect=ValueError("invalid configuration")),
    )

    with pytest.raises(SystemExit, match="invalid configuration"):
        runtime.main(["serve"])
