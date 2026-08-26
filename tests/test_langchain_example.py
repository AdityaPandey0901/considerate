"""E3: prove examples/langchain_tool_example.py actually works against a
real langchain-core install, rather than trusting an import-guarded example
that could silently bit-rot — it's cited in the README as *the* tool-
calling integration story.
"""

import importlib
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("langchain_core")

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
        return httpx.Response(404)
    return httpx.Response(200, content=b"<html>hello from mock</html>")


@pytest.fixture
def example_module(monkeypatch):
    # The example builds its ConsiderateClient singleton at import time with
    # no transport override — patch httpx.Client before importing so it
    # picks up the mock transport, same trick test_cli.py uses.
    real_client_cls = httpx.Client

    def patched(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(_handler))
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)

    sys.path.insert(0, str(EXAMPLES_DIR))
    sys.modules.pop("langchain_tool_example", None)
    try:
        yield importlib.import_module("langchain_tool_example")
    finally:
        sys.modules.pop("langchain_tool_example", None)
        sys.path.remove(str(EXAMPLES_DIR))


def test_module_builds_a_real_langchain_tool(example_module):
    from langchain_core.tools import BaseTool

    assert example_module.fetch_page_tool is not None
    assert isinstance(example_module.fetch_page_tool, BaseTool)


def test_fetch_page_returns_page_content(example_module):
    result = example_module.fetch_page("https://langchainexample.test/page")
    assert "hello from mock" in result


def test_tool_invoke_round_trip_through_langchain(example_module):
    # Exercises the actual LangChain BaseTool.invoke() path, not just the
    # bare function — proving the decorator wiring (name/schema inference)
    # works, not only the underlying callable.
    result = example_module.fetch_page_tool.invoke({"url": "https://langchainexample.test/page"})
    assert "hello from mock" in result


def test_circuit_open_produces_a_plain_language_tool_result(example_module, monkeypatch):
    from considerate.exceptions import CircuitOpenError

    def raise_circuit_open(url, **kwargs):
        raise CircuitOpenError("langchainexample.test", "http_503", 12.0)

    monkeypatch.setattr(example_module._client, "get", raise_circuit_open)
    result = example_module.fetch_page("https://langchainexample.test/page")
    assert "PAUSED" in result
    assert "langchainexample.test" in result
