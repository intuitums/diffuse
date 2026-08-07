"""A narrowly scoped MCP server for a pinned review-tool provider."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

from service.agents.mcp_bridge import BRIDGE_URL_VARIABLE, MAX_SEARCH_LIMIT
from service.agents.tool_api import DEFAULT_AGENT_TOOL_PORT
from service.review.tools import ReviewToolProvider

AGENT_TOOL_URL_VARIABLE = "DIFFUSE_AGENT_TOOL_URL"
SESSION_CAPABILITY_VARIABLE = "DIFFUSE_AGENT_SESSION_CAPABILITY"
_ALLOWED_TOOL_HOSTS = frozenset({"app", "127.0.0.1", "localhost"})


def create_tool_server(provider: ReviewToolProvider) -> FastMCP:
    """Expose only the agent profile's index-backed `search_code` capability.

    The bridge owns the provider's lifetime.  This factory has no database,
    process, or global state, so tests can exercise the exact tool binding.
    """

    server = FastMCP("Diffuse review tools")

    @server.tool()
    def search_code(
        query: str, path_prefix: str | None = None, limit: int = 8
    ) -> dict[str, object]:
        """Search only the review's pinned immutable index snapshots."""

        return provider.search_code(query, path_prefix=path_prefix, limit=limit)

    return server


def create_bridge_tool_server(bridge_url: str) -> FastMCP:
    """Create the stdio child which forwards a tool call to its parent bridge."""

    server = FastMCP("Diffuse review tools")

    @server.tool()
    def search_code(
        query: str, path_prefix: str | None = None, limit: int = 8
    ) -> dict[str, object]:
        """Search only the parent review's pinned immutable index snapshots."""

        # Clamped here as well as in the bridge. This side keeps the agent's own
        # error message useful; the bridge's copy is the one that is load-bearing,
        # because this process is the one reading untrusted content.
        payload = json.dumps(
            {
                "query": query,
                "path_prefix": path_prefix,
                "limit": min(max(limit, 1), MAX_SEARCH_LIMIT),
            }
        ).encode()
        request = Request(
            f"{bridge_url}/search_code",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback bridge URL
            result = json.loads(response.read())
        if not isinstance(result, dict):
            raise RuntimeError("Diffuse review tool bridge returned a non-object result")
        return result

    return server


def validate_agent_tool_url(tool_url: str) -> str:
    """Refuse anything that is not the control-plane's internal agent-tool URL."""

    parsed = urlparse(tool_url)
    path = parsed.path.rstrip("/") or "/"
    port = parsed.port or (80 if parsed.scheme == "http" else None)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_TOOL_HOSTS
        or port != DEFAULT_AGENT_TOOL_PORT
        or path != "/agent/v1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{AGENT_TOOL_URL_VARIABLE} must be an http URL to app:"
            f"{DEFAULT_AGENT_TOOL_PORT}/agent/v1 on the private agent network"
        )
    return f"{parsed.scheme}://{parsed.hostname}:{port}/agent/v1"


def create_capability_tool_server(tool_url: str, capability: str) -> FastMCP:
    """Create the stdio child for a runner session's remote capability tools.

    The runner receives the short-lived capability from the control plane only
    when it eventually starts a session. It is inherited by this child rather
    than placed in the MCP config or an argv, where other local processes could
    read it. The control plane verifies the signature and exact operation on
    every call; this child is deliberately just a transport adapter.
    """

    endpoint = f"{validate_agent_tool_url(tool_url)}/tools/search-code"
    server = FastMCP("Diffuse review tools")

    @server.tool()
    def search_code(
        query: str, path_prefix: str | None = None, limit: int = 8
    ) -> dict[str, object]:
        """Search only the review's capability-pinned immutable index snapshot."""

        payload = json.dumps(
            {
                "query": query,
                "path": path_prefix,
                "limit": min(max(limit, 1), MAX_SEARCH_LIMIT),
            },
            separators=(",", ":"),
        ).encode()
        request = Request(
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - allowlisted private URL
            result = json.loads(response.read())
        if not isinstance(result, dict):
            raise RuntimeError("Diffuse capability tool endpoint returned a non-object result")
        return result

    return server


def main() -> None:
    argparse.ArgumentParser(description="Diffuse review-tool MCP bridge").parse_args()
    # Credentials travel in the environment, never argv (`ps` would otherwise
    # expose them): the loopback bridge URL embeds a bearer path token, and the
    # capability transport carries DIFFUSE_AGENT_SESSION_CAPABILITY separately.
    bridge_url = os.environ.get(BRIDGE_URL_VARIABLE)
    if bridge_url:
        create_bridge_tool_server(bridge_url).run(transport="stdio")
        return

    tool_url = os.environ.get(AGENT_TOOL_URL_VARIABLE)
    capability = os.environ.get(SESSION_CAPABILITY_VARIABLE)
    if tool_url and capability:
        create_capability_tool_server(tool_url, capability).run(transport="stdio")
        return

    raise SystemExit(
        f"{BRIDGE_URL_VARIABLE} or ({AGENT_TOOL_URL_VARIABLE} and "
        f"{SESSION_CAPABILITY_VARIABLE}) must be set; this server is spawned "
        "by a Diffuse agent session and is not usable on its own"
    )


if __name__ == "__main__":
    main()
