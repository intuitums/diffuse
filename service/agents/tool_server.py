"""A narrowly scoped MCP server for a pinned review-tool provider."""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

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

        payload = json.dumps({"query": query, "path_prefix": path_prefix, "limit": limit}).encode()
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
    parser = argparse.ArgumentParser(description="Diffuse review-tool MCP bridge")
    parser.add_argument("--bridge-url", required=True)
    arguments = parser.parse_args()
    create_bridge_tool_server(arguments.bridge_url).run(transport="stdio")


if __name__ == "__main__":
    main()
