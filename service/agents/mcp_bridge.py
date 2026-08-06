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
                    result = provider.search_code(
                        payload["query"],
                        path_prefix=payload.get("path_prefix"),
                        limit=payload.get("limit", 8),
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


def write_mcp_config(path: Path, *, bridge_url: str) -> Path:
    """Write the stdio MCP declaration consumed by a single Claude session.

    Keeping this config creation here prevents a general user MCP config from
    entering a review session; Claude receives it together with
    `--strict-mcp-config` and no other server declarations.
    """

    document = {
        "mcpServers": {
            TOOL_SERVER_NAME: {
                "command": sys.executable,
                "args": ["-m", "service.agents.tool_server", "--bridge-url", bridge_url],
            }
        }
    }
    path.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n")
    return path
