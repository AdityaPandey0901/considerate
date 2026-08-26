"""The same wrapper pattern as `langchain_tool_example.py`, for CrewAI.

One `ConsiderateClient` shared across every tool call an agent/crew makes,
so per-domain rate/circuit-breaker state persists across the whole run
instead of resetting per call.

`crewai` is not a dependency of `considerate` — install it separately to
run this file (`pip install crewai`). Not covered by this repo's automated
tests: crewai pulls in a large dependency tree (chromadb, onnxruntime,
kubernetes client, ...) disproportionate to testing one thin wrapper that
is otherwise identical to the already-tested LangChain example — see
tests/test_langchain_example.py for the equivalent proof-of-integration
against a real framework install.
"""

from __future__ import annotations

from considerate import AgentIdentity, CircuitOpenError, ConsiderateClient, DisallowedError

_client = ConsiderateClient(
    identity=AgentIdentity(
        name="CrewAIScraperAgent",
        version="0.1",
        contact="mailto:you@example.com",
        intent="bulk-scrape",
    )
)


def fetch_page(url: str) -> str:
    """Fetch a URL and return its text content, or a plain-language
    explanation if the target site looks like it's struggling or has
    disallowed the path — see `langchain_tool_example.py` for the same
    logic with more detailed comments.
    """
    try:
        response = _client.get(url)
        response.raise_for_status()
        return response.text
    except CircuitOpenError as e:
        return (
            f"PAUSED: stopped fetching from {e.domain} because it looked like it was "
            f"struggling ({e.reason}). Will be safe to retry after {e.retry_after:.0f}s."
        )
    except DisallowedError as e:
        return f"SKIPPED: {e.url} is disallowed by the site's robots.txt."


try:
    from crewai.tools import tool

    fetch_page_tool = tool("Fetch a web page")(fetch_page)
except ImportError:
    fetch_page_tool = None  # crewai not installed; fetch_page still usable directly


if __name__ == "__main__":
    for url in [
        "https://example.com/docs/1",
        "https://example.com/docs/2",
    ]:
        print(url, "->", fetch_page(url)[:80])
