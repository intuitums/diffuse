"""Deterministic cassette record/replay for agent subprocess transcripts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from service.agents.errors import AgentSessionError


@dataclass(frozen=True)
class SessionTranscript:
    """The bounded observable result of a CLI session."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def request_key(self) -> str:
        payload = json.dumps(list(self.argv), separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


def record(path: Path, transcript: SessionTranscript) -> None:
    """Write one explicit cassette; callers choose where test fixtures live."""

    document = {"version": 1, "request_key": transcript.request_key, **asdict(transcript)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def replay(path: Path, argv: list[str]) -> SessionTranscript:
    """Load a cassette only when it matches the requested invocation exactly."""

    try:
        raw = json.loads(path.read_text())
        transcript = SessionTranscript(
            argv=tuple(raw["argv"]),
            returncode=int(raw["returncode"]),
            stdout=str(raw["stdout"]),
            stderr=str(raw["stderr"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AgentSessionError(f"Agent session cassette is unreadable: {path}") from error
    requested = SessionTranscript(tuple(argv), 0, "", "")
    if raw.get("version") != 1 or raw.get("request_key") != requested.request_key:
        raise AgentSessionError("Agent session cassette does not match this invocation")
    return transcript
