"""Tests for the MCP server wrapper (E1). `@server.tool()` leaves the
decorated function directly callable (verified against the installed SDK
version), so these call the registered tool functions the same way an MCP
client invocation ultimately would, without needing a full stdio
client/server round trip.
"""

import httpx
import pytest

pytest.importorskip("mcp.server.mcpserver")

from considerate.mcp_server import considerate_status, fetch, server  # noqa: E402


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
        return httpx.Response(404)
    return httpx.Response(200, content=b"mcp fetch result")


@pytest.fixture(autouse=True)
def _mock_transport(monkeypatch):
    from considerate import mcp_server

    monkeypatch.setattr(mcp_server._client, "_httpx", httpx.Client(transport=httpx.MockTransport(_handler)))


def test_server_is_named_considerate():
    assert server.name == "considerate"


def test_fetch_tool_returns_page_content():
    result = fetch("https://mcptest.test/page")
    assert result == "mcp fetch result"


def test_fetch_tool_reports_circuit_open_in_plain_language(monkeypatch):
    from considerate.exceptions import CircuitOpenError
    from considerate import mcp_server

    def raise_circuit_open(url, **kwargs):
        raise CircuitOpenError("mcptest.test", "http_503", 30.0)

    monkeypatch.setattr(mcp_server._client, "get", raise_circuit_open)
    result = fetch("https://mcptest.test/page")
    assert "PAUSED" in result
    assert "mcptest.test" in result


def test_considerate_status_reports_snapshot():
    fetch("https://mcptest.test/page")
    status = considerate_status()
    assert "mcptest.test" in status
    assert status["mcptest.test"]["circuit_state"] == "closed"


def test_considerate_status_for_unknown_domain():
    status = considerate_status("never-fetched.test")
    assert status["status"] == "not yet fetched"
