"""An MCP server exposing considerate as a `fetch` tool, so any
MCP-compatible agent (Claude Desktop, or any other MCP host) gets
rate-limited, circuit-broken fetching "for free" — no wrapper code, per
SPEC.md's roadmap ("MCP server wrapper... following the same 'capability,
not config' philosophy as the rest of the protocol").

Run directly:

    python -m considerate.mcp_server

or via the installed console script:

    considerate-mcp

Requires the optional `mcp` dependency: `pip install considerate[mcp]`.
Targets the MCP Python SDK's v2 API (`mcp.server.mcpserver.MCPServer`) —
v1's `FastMCP` was renamed; see the SDK's own migration guide if you're
pinned to `mcp<2`.
"""

from __future__ import annotations

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover - exercised via the error message
    raise ImportError("considerate.mcp_server requires the 'mcp' package: pip install considerate[mcp]") from exc

from ._version import __version__
from .client import ConsiderateClient
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity

server = MCPServer("considerate")

_client = ConsiderateClient(identity=AgentIdentity(name="considerate-mcp", version=__version__, intent="browse"))


@server.tool()
def fetch(url: str) -> str:
    """Fetch a URL, throttled per the target site's declared or inferred
    capacity, with a circuit breaker that stops entirely if the site looks
    like it's struggling.

    Returns the page's text content on success. On failure, returns a
    plain-language explanation — never a raw stack trace or exception —
    so the calling agent can relay it honestly to a user instead of
    retrying blindly or failing silently.
    """
    try:
        response = _client.get(url)
        response.raise_for_status()
        return response.text
    except CircuitOpenError as e:
        return (
            f"PAUSED: stopped fetching from {e.domain} because it looked like it was "
            f"struggling ({e.reason}). Safe to retry after {e.retry_after:.0f}s. Tell "
            "the user this site is being skipped for now rather than retrying immediately."
        )
    except DisallowedError as e:
        return f"SKIPPED: {e.url} is disallowed by the site's robots.txt."


@server.tool()
def considerate_status(domain: str | None = None) -> dict:
    """Report what considerate currently sees: discovered policy, current
    rate, and circuit breaker state for every domain fetched so far in this
    session — or just one domain, if given.
    """
    snapshot = _client.snapshot()
    if domain is not None:
        return snapshot.get(domain, {"domain": domain, "status": "not yet fetched"})
    return snapshot


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
