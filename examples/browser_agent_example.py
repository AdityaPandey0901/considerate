"""The integration browser agents (browser-use, Stagehand, raw Playwright)
actually need: throttling real page navigations, not HTTP calls a browser
agent never makes directly.

Requires the optional playwright dependency: pip install considerate[playwright]
(and `playwright install chromium` once, if you haven't already).

Run: python examples/browser_agent_example.py
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from considerate import AgentIdentity, CircuitOpenError, DisallowedError
from considerate.browser import ConsiderateBrowserPage


def main() -> None:
    identity = AgentIdentity(
        name="ExampleBrowserAgent",
        version="1.0",
        contact="mailto:you@example.com",
        intent="bulk-scrape",
    )

    urls = [f"https://example.com/documents/{i}" for i in range(1, 6)]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        considerate_page = ConsiderateBrowserPage(
            page,
            identity=identity,
            on_event=lambda e: print(f"[considerate] {e.kind} domain={e.domain} {e.data}"),
        )

        for url in urls:
            try:
                considerate_page.goto(url)
                print(url, "->", page.title())
            except DisallowedError:
                print(url, "-> skipped (robots.txt disallows this path)")
            except CircuitOpenError as e:
                print(f"Stopping: {e.payload}")
                break

        print("\nsnapshot:", considerate_page.snapshot())
        considerate_page.close()
        browser.close()


if __name__ == "__main__":
    main()
