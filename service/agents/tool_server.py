"""A narrowly scoped MCP server for a pinned review-tool provider."""

from __future__ import annotations

import argparse
import json
import os
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

from service.agents.mcp_bridge import BRIDGE_URL_VARIABLE, MAX_SEARCH_LIMIT
from service.review.tools import ReviewToolProvider


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


def main() -> None:
    argparse.ArgumentParser(description="Diffuse review-tool MCP bridge").parse_args()
    # Read from the environment, not a flag: the URL embeds the session's bearer
    # token, and a flag would put that token in `ps` output for every local user.
    bridge_url = os.environ.get(BRIDGE_URL_VARIABLE)
    if not bridge_url:
        raise SystemExit(
            f"{BRIDGE_URL_VARIABLE} is not set; this server is spawned by a "
            "Diffuse agent session and is not usable on its own"
        )
    create_bridge_tool_server(bridge_url).run(transport="stdio")


if __name__ == "__main__":
    main()
