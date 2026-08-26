"""Standalone script run via subprocess by test_browser.py, not collected
by pytest directly. Playwright's async driver bootstrap conflicts with
pytest-asyncio's event-loop management as soon as *any* other async test
has run earlier in the same session (confirmed: this exact scenario passes
every time stand-alone, and fails intermittently in-process depending on
test order) — running it in a fresh interpreter sidesteps that entirely
rather than fighting pytest-asyncio/Playwright version pins.
"""

from __future__ import annotations

import asyncio
import sys


async def main(port: int) -> None:
    from playwright.async_api import async_playwright

    from considerate import AgentIdentity
    from considerate.browser import AsyncConsiderateBrowserPage

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        considerate_page = AsyncConsiderateBrowserPage(page, identity=AgentIdentity(name="AsyncBrowserBot"))

        response = await considerate_page.goto(f"http://127.0.0.1:{port}/asyncpage")
        assert response.status == 200, f"expected 200, got {response.status}"

        snap = considerate_page.snapshot()
        assert "127.0.0.1" in snap, f"domain missing from snapshot: {snap!r}"

        await considerate_page.aclose()
        await browser.close()

    print("ASYNC_BROWSER_TEST_OK")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
