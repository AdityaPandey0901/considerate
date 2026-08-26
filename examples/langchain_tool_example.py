"""Wrap ConsiderateClient as an agent tool, so a framework-driven agent given
an open-ended task ("scrape all 400 pages linked from this index") self-limits
without the developer ever configuring a rate limit.

This uses LangChain's `@tool` decorator as the illustration since it's the
most common agent framework, but the pattern is identical for CrewAI,
browser-use custom actions, or any framework that accepts a plain callable:
one shared ConsiderateClient, reused across every call the agent makes, so
per-domain state (rate, circuit breaker) persists across the whole task
instead of resetting per call.

`langchain` is not a dependency of `considerate` — install it separately to
run this file (`pip install langchain-core`).
"""

from __future__ import annotations

from considerate import AgentIdentity, CircuitOpenError, ConsiderateClient, DisallowedError

# One client for the whole agent run — this is what makes rate/circuit state
# carry across every tool call the agent makes, not just within one call.
_client = ConsiderateClient(
    identity=AgentIdentity(
        name="LangChainScraperAgent",
        version="0.1",
        contact="mailto:you@example.com",
        intent="bulk-scrape",
    )
)


def fetch_page(url: str) -> str:
    """Fetch a URL and return its text content.

    Automatically rate-limited per the target site's declared or inferred
    capacity. If the site appears to be struggling, this returns a clear
    message instead of raising into the agent loop uncaught — so the LLM
    can decide what to tell the user, rather than the run just crashing.
    """
    try:
        response = _client.get(url)
        response.raise_for_status()
        return response.text
    except CircuitOpenError as e:
        return (
            f"PAUSED: stopped fetching from {e.domain} because it looked like it was "
            f"struggling ({e.reason}). Will be safe to retry after {e.retry_after:.0f}s. "
            "Consider telling the user this site is being skipped for now rather than "
            "retrying immediately."
        )
    except DisallowedError as e:
        return f"SKIPPED: {e.url} is disallowed by the site's robots.txt."


# --- LangChain wiring -------------------------------------------------------
try:
    from langchain_core.tools import tool

    fetch_page_tool = tool(fetch_page)
except ImportError:
    fetch_page_tool = None  # langchain-core not installed; fetch_page still usable directly


if __name__ == "__main__":
    urls = [
        "https://example.com/docs/1",
        "https://example.com/docs/2",
        "https://example.com/docs/3",
    ]
    for url in urls:
        print(url, "->", fetch_page(url)[:80])
