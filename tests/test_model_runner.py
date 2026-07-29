import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from service.model_config import ModelExecutor, ModelTarget, StructuredOutputMode
from service.model_execution import (
    ExecutorNotAuthenticatedError,
    ModelRateLimitedError,
    StructuredGenerationRequest,
)
from service.model_runner import (
    FakeCLIBackend,
    _RunnerServer,
    cli_environment,
    run_supervised_process,
)
from service.model_runner_adapters import (
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    ProductionCLIBackend,
    SupervisedProcessResult,
    _classified_failure,
    _codex_app_server_environment,
    _strict_output_schema,
)
from service.model_runner_client import (
    RunnerStructuredGenerator,
    cancel_runner_request,
    runner_health,
)
from service.model_runner_protocol import RunnerGenerateRequest, RunnerGenerateSuccess
from service.review_engine import ModelConnectionProbe


def _wire_request(executor: str) -> RunnerGenerateRequest:
    return RunnerGenerateRequest(
        request_id=f"{executor}-review",
        executor=executor,
        model="default",
        workload="review_candidate",
        system_prompt="Apply Diffuse review policy.",
        user_prompt="Prepared untrusted diff.",
        output_schema={
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
            "required": ["ready"],
            "additionalProperties": False,
        },
        max_output_tokens=100,
        deadline_unix_ms=int((time.time() + 10) * 1000),
    )


def test_runner_protocol_has_no_workspace_or_publication_escape_hatch():
    request = RunnerGenerateRequest(
        request_id="review-1",
        executor="codex-cli",
        model="default",
        workload="review_candidate",
        system_prompt="Diffuse instructions",
        user_prompt="Prepared diff",
        output_schema={"type": "object"},
        max_output_tokens=100,
        deadline_unix_ms=int((time.time() + 10) * 1000),
    )

    assert request.workspace_access == "none"
    assert request.publication_authority is False
    assert "cwd" not in RunnerGenerateRequest.model_fields
    assert "environment" not in RunnerGenerateRequest.model_fields
    with pytest.raises(ValueError):
        RunnerGenerateRequest.model_validate(
            {
                **request.model_dump(),
                "cwd": "/repository",
            }
        )


def test_cli_environment_is_allowlist_based():
    environment = cli_environment(
        {
            "HOME": "/Users/tester",
            "PATH": "/usr/bin",
            "LANG": "en_US.UTF-8",
            "OPENAI_API_KEY": "provider-secret",
            "ANTHROPIC_AUTH_TOKEN": "provider-token",
            "DATABASE_URL": "postgresql://secret",
            "GITHUB_APP_PRIVATE_KEY": "private-key",
            "UNRELATED": "value",
        }
    )

    assert environment == {
        "HOME": "/Users/tester",
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
    }


def test_supervised_process_runs_in_empty_directory_without_diffuse_secrets():
    script = (
        "import json, os; "
        "print(json.dumps({'files': os.listdir('.'), "
        "'home': os.environ.get('HOME'), "
        "'key': os.environ.get('OPENAI_API_KEY'), "
        "'db': os.environ.get('DATABASE_URL')}))"
    )
    result = run_supervised_process(
        [sys.executable, "-c", script],
        stdin=b"",
        timeout_seconds=5,
        environment={
            "HOME": "/Users/tester",
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "secret",
            "DATABASE_URL": "postgresql://secret",
        },
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload == {
        "files": [],
        "home": "/Users/tester",
        "key": None,
        "db": None,
    }


def test_supervised_process_materializes_only_runner_owned_files():
    script = "from pathlib import Path; print(Path('schema.json').read_text())"
    result = run_supervised_process(
        [sys.executable, "-c", script],
        stdin=b"",
        timeout_seconds=5,
        files={"schema.json": b'{"type":"object"}'},
    )

    assert result.stdout.strip() == b'{"type":"object"}'
    with pytest.raises(ValueError, match="file name"):
        run_supervised_process(
            [sys.executable, "-c", "pass"],
            stdin=b"",
            timeout_seconds=5,
            files={"../schema.json": b"{}"},
        )


def test_supervised_process_honors_runner_cancellation():
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(InterruptedError, match="cancelled"):
        run_supervised_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=b"",
            timeout_seconds=10,
            cancellation_event=cancelled,
        )


def test_supervised_process_stops_oversized_output():
    with pytest.raises(ValueError, match="output"):
        run_supervised_process(
            [sys.executable, "-c", "print('x' * 1000000)"],
            stdin=b"",
            timeout_seconds=5,
            max_output_bytes=1024,
        )


def test_codex_adapter_uses_isolated_schema_mode(monkeypatch):
    monkeypatch.setattr(
        "service.model_runner_adapters._resolve_executable",
        lambda configured, display_name: "/usr/local/bin/codex",
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._version_and_help",
        lambda *args, **kwargs: "codex-cli 1.2.3",
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._probe_authentication",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._probe_codex_app_server_schema",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._probe_codex_disableable_features",
        lambda *args, **kwargs: None,
    )
    captured = {"messages": []}

    class FakeSession:
        def __init__(self, executable, **kwargs):
            captured["session"] = kwargs

        def send(self, payload):
            captured["messages"].append(payload)

        def wait_for_response(self, response_id, notification):
            if response_id == 1:
                return {}
            if response_id == 2:
                return {
                    "thread": {"id": "thread-1"},
                    "instructionSources": [],
                }
            notification(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": '{"ready":true}',
                        }
                    },
                }
            )
            notification(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 12,
                                "outputTokens": 3,
                            }
                        }
                    },
                }
            )
            notification(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                        }
                    },
                }
            )
            return {"turn": {"id": "turn-1"}}

        def receive(self):
            raise AssertionError("turn completed before the response")

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "service.model_runner_adapters._CodexAppServerSession",
        FakeSession,
    )
    adapter = CodexCLIAdapter()
    result = adapter.generate(_wire_request("codex-cli"), threading.Event())

    assert result.value == {"ready": True}
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 3
    assert result.executor_version == "codex-cli 1.2.3"
    thread_start = captured["messages"][2]
    assert thread_start["params"]["ephemeral"] is True
    assert thread_start["params"]["approvalPolicy"] == "on-request"
    assert thread_start["params"]["approvalsReviewer"] == "auto_review"
    assert thread_start["params"]["developerInstructions"].startswith(
        "Apply Diffuse review policy."
    )
    turn_start = captured["messages"][3]
    assert turn_start["params"]["input"] == [
        {"type": "text", "text": "Prepared untrusted diff."}
    ]
    assert turn_start["params"]["approvalPolicy"] == "on-request"
    assert turn_start["params"]["approvalsReviewer"] == "auto_review"
    assert turn_start["params"]["sandboxPolicy"]["type"] == "readOnly"
    assert turn_start["params"]["sandboxPolicy"]["access"]["type"] == "restricted"
    assert turn_start["params"]["outputSchema"]["type"] == "object"
    assert captured["closed"] is True


def test_claude_adapter_disables_tools_settings_and_persistence(monkeypatch):
    monkeypatch.setattr(
        "service.model_runner_adapters._resolve_executable",
        lambda configured, display_name: "/usr/local/bin/claude",
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._version_and_help",
        lambda *args, **kwargs: "2.1.220 (Claude Code)",
    )
    monkeypatch.setattr(
        "service.model_runner_adapters._probe_authentication",
        lambda *args, **kwargs: None,
    )
    captured = {}

    def fake_process(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SupervisedProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "is_error": False,
                    "structured_output": {"ready": True},
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 2,
                        "output_tokens": 4,
                    },
                    "modelUsage": {"claude-sonnet-test": {}},
                    "stop_reason": "end_turn",
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("service.model_runner_adapters.run_supervised_process", fake_process)
    adapter = ClaudeCLIAdapter()
    result = adapter.generate(_wire_request("claude-cli"), threading.Event())

    assert result.value == {"ready": True}
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 4
    assert result.resolved_model == "claude-sonnet-test"
    assert "--safe-mode" in captured["command"]
    assert "--no-session-persistence" in captured["command"]
    assert (
        captured["command"][captured["command"].index("--permission-mode") + 1]
        == "auto"
    )
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert captured["command"][captured["command"].index("--setting-sources") + 1] == ""
    assert "Prepared untrusted diff." not in captured["command"]
    assert captured["stdin"] == b"Prepared untrusted diff."
    assert (
        b"Apply Diffuse review policy."
        in captured["files"]["system-prompt.txt"]
    )


def test_cli_failure_classification_never_returns_raw_output():
    error = _classified_failure(
        display_name="Claude Code CLI",
        returncode=1,
        stdout=b"You've hit your weekly limit; secret-payload",
        stderr=b"",
        status_code=429,
    )

    assert isinstance(error, ModelRateLimitedError)
    assert str(error) == "Claude Code CLI is rate limited"
    assert "secret-payload" not in str(error)


def test_codex_schema_is_strict_without_changing_the_wire_contract():
    source = {
        "type": "object",
        "properties": {
            "ready": {"type": "boolean"},
            "default": {"type": "string"},
            "note": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            },
        },
        "required": ["ready"],
    }

    strict = _strict_output_schema(source)

    assert strict["additionalProperties"] is False
    assert strict["required"] == ["ready", "default", "note"]
    assert strict["properties"]["default"] == {"type": "string"}
    assert "default" not in strict["properties"]["note"]
    assert "additionalProperties" not in source
    assert source["required"] == ["ready"]


def test_codex_app_server_home_exposes_auth_but_not_user_configuration(
    monkeypatch,
    tmp_path,
):
    original = tmp_path / "original"
    isolated = tmp_path / "isolated"
    original.mkdir()
    isolated.mkdir()
    (original / "auth.json").write_text("{}")
    (original / "config.toml").write_text("[mcp_servers.untrusted]")
    (original / "skills").mkdir()
    monkeypatch.setattr(
        "service.model_runner_adapters.cli_environment",
        lambda source=None: {
            "HOME": str(tmp_path),
            "CODEX_HOME": str(original),
            "PATH": "/usr/bin",
        },
    )

    environment = _codex_app_server_environment(isolated)

    assert environment["CODEX_HOME"] == str(isolated)
    assert (isolated / "auth.json").is_symlink()
    assert (isolated / "auth.json").resolve() == original / "auth.json"
    assert not (isolated / "config.toml").exists()
    assert not (isolated / "skills").exists()


def test_production_backend_enables_only_probed_authenticated_adapters(monkeypatch):
    class ReadyCodex:
        version = "codex-cli test"

        def __init__(self, executable):
            pass

        def generate(self, request, cancellation_event):
            return RunnerGenerateSuccess(
                request_id=request.request_id,
                value={"ready": True},
                executor_version=self.version,
            )

    class LoggedOutClaude:
        def __init__(self, executable):
            raise ExecutorNotAuthenticatedError("Claude Code CLI is not authenticated")

    monkeypatch.setattr("service.model_runner_adapters.CodexCLIAdapter", ReadyCodex)
    monkeypatch.setattr("service.model_runner_adapters.ClaudeCLIAdapter", LoggedOutClaude)
    backend = ProductionCLIBackend()

    assert backend.supported_executors == ("codex-cli",)
    assert backend.generate(_wire_request("codex-cli")).value == {"ready": True}
    with pytest.raises(ExecutorNotAuthenticatedError):
        backend.generate(_wire_request("claude-cli"))


def test_runner_preserves_authentication_failure_code():
    class UnauthenticatedBackend:
        supported_executors = ()

        def generate(self, request):
            raise ExecutorNotAuthenticatedError("CLI login is required")

        def cancel(self, request_id):
            return False

    with tempfile.TemporaryDirectory(prefix="diffuse-runner-test-", dir="/tmp") as directory:
        socket_path = Path(directory) / "runner.sock"
        server = _RunnerServer(str(socket_path), UnauthenticatedBackend(), capacity=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            generator = RunnerStructuredGenerator(socket_path)
            with pytest.raises(ExecutorNotAuthenticatedError, match="login"):
                generator.generate(
                    StructuredGenerationRequest(
                        request_id="auth-review",
                        workload="review_candidate",
                        response_model=ModelConnectionProbe,
                        system_prompt="Diffuse connectivity fixture",
                        user_prompt="Return ready",
                        target=ModelTarget(
                            executor=ModelExecutor.CODEX_CLI,
                            requested_model="default",
                            structured_output_mode=StructuredOutputMode.SCHEMA,
                        ),
                        max_output_tokens=32,
                        timeout_seconds=5,
                        idempotency_key="auth-review",
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_fake_runner_round_trip_is_schema_validated():
    with tempfile.TemporaryDirectory(prefix="diffuse-runner-test-", dir="/tmp") as directory:
        socket_path = Path(directory) / "runner.sock"
        server = _RunnerServer(str(socket_path), FakeCLIBackend(), capacity=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = runner_health(socket_path)
            assert "codex-cli" in health.supported_executors
            assert not cancel_runner_request(socket_path, "not-active")

            generator = RunnerStructuredGenerator(socket_path)
            result = generator.generate(
                StructuredGenerationRequest(
                    request_id="fixture-review",
                    workload="review_candidate",
                    response_model=ModelConnectionProbe,
                    system_prompt="Diffuse connectivity fixture",
                    user_prompt='FAKE_RESPONSE:{"ready":true}',
                    target=ModelTarget(
                        executor=ModelExecutor.CODEX_CLI,
                        requested_model="default",
                        structured_output_mode=StructuredOutputMode.SCHEMA,
                    ),
                    max_output_tokens=32,
                    timeout_seconds=5,
                    idempotency_key="fixture-review",
                )
            )
            assert result.value.ready is True
            assert result.executor_version == "fake-cli/1"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
