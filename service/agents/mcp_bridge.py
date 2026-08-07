"""Per-session MCP configuration for the Diffuse review-tool bridge."""

from __future__ import annotations

import json
import secrets
import sys
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from service.review.tools import ReviewToolProvider

TOOL_SERVER_NAME = "diffuse-review-tools"

#: The bridge URL embeds a bearer token in its path, so it is a credential and
#: travels like one: through the child's environment, never through argv, where
#: `ps` would show it to every other user on the machine.
BRIDGE_URL_VARIABLE = "DIFFUSE_REVIEW_TOOL_BRIDGE_URL"

#: The most matches a single `search_code` call may ask the index for. The agent
#: chooses `limit`, and an untrusted diff is what steers the agent, so the value
#: is clamped rather than trusted.
MAX_SEARCH_LIMIT = 20


class McpBridge(AbstractContextManager["McpBridge"]):
    """Serve one provider to its one stdio MCP child over loopback only."""

    def __init__(self, provider: ReviewToolProvider) -> None:
        self._provider = provider
        self._token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("MCP bridge has not started")
        return f"http://127.0.0.1:{self._server.server_port}/{self._token}"

    def __enter__(self) -> McpBridge:
        provider = self._provider
        token = self._token

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != f"/{token}/search_code":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload: Any = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
                        raise ValueError("query is required")
                    limit = payload.get("limit", 8)
                    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                        raise ValueError("limit must be a positive integer")
                    result = provider.search_code(
                        payload["query"],
                        path_prefix=payload.get("path_prefix"),
                        limit=min(limit, MAX_SEARCH_LIMIT),
                    )
                    body = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    self.send_error(400, str(error))

            def log_message(self, _format: str, *_args: object) -> None:
                """Keep an agent's tool calls out of the parent process stderr."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        # Cleared so a reused bridge fails loudly on `url` rather than handing
        # out the address of a socket that is already closed.
        self._server = None
        self._thread = None


def write_mcp_config(path: Path, *, bridge_url: str | None) -> Path:
    """Write the stdio MCP declaration consumed by a single Claude session.

    Always written, including for a session with no tools, because the file is
    half of a boundary rather than a convenience: paired with
    `--strict-mcp-config` it is what stops Claude loading the `.mcp.json` of the
    repository under review, which is the untrusted input. `bridge_url=None`
    therefore produces an empty server map -- an explicit "no servers" -- rather
    than no file.

    The bridge URL carries the session's bearer token, so it is passed to the
    child through `BRIDGE_URL_VARIABLE` in the environment instead of being
    written into an argv this file would then leak to `ps`.
    """

    servers: dict[str, object] = {}
    if bridge_url is not None:
        servers[TOOL_SERVER_NAME] = {
            "command": sys.executable,
            "args": ["-m", "service.agents.tool_server"],
        }
    path.write_text(
        json.dumps({"mcpServers": servers}, separators=(",", ":"), sort_keys=True) + "\n"
    )
    path.chmod(0o600)
    return path
