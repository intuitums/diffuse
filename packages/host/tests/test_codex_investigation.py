from __future__ import annotations

import json
from pathlib import Path

import pytest
from diffuse_host.codex import build_argv, parse_envelope
from diffuse_host.errors import (
    AgentInvestigationOutputError,
    AgentInvestigationRateLimited,
    AgentInvestigationTerminalError,
)
from diffuse_protocol.profiles import REVIEW


def test_codex_exec_is_json_schema_bound_and_read_only():
    argv = build_argv(
        Path("/usr/bin/codex"),
        profile=REVIEW,
        workspace=Path("/work"),
        schema={"type": "object"},
        system_prompt="system",
        user_prompt="review",
        mcp_config=Path("/mcp.json"),
    )
    assert argv[:3] == ["/usr/bin/codex", "exec", "--json"]
    assert "--output-schema" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--mcp-config") + 1] == "/mcp.json"


def test_codex_completed_event_becomes_shared_result():
    envelope = parse_envelope(
        json.dumps(
            {
                "type": "turn.completed",
                "result": {"findings": []},
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }
        )
    )
    assert envelope.result == {"findings": []}
    assert envelope.prompt_tokens == 2
    assert envelope.completion_tokens == 3


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("rate limit reached", AgentInvestigationRateLimited),
        ("authentication required", AgentInvestigationTerminalError),
    ],
)
def test_codex_classifies_terminal_and_retryable_events(message, error):
    with pytest.raises(error):
        parse_envelope(json.dumps({"type": "error", "message": message}))


def test_codex_requires_a_completed_result():
    with pytest.raises(AgentInvestigationOutputError):
        parse_envelope(json.dumps({"type": "thread.started"}))
