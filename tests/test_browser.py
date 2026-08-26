"""Tests for the Playwright browser wrapper (C5) — a real Chromium instance
against a real local HTTP server, no mocks. This is the integration this
library was missing: an agent driving an actual browser, not making HTTP
calls directly.
"""

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api")
playwright_async_api = pytest.importorskip("playwright.async_api")

from considerate import AgentIdentity, CircuitOpenError, ConsiderateConfig, DisallowedError  # noqa: E402
from considerate.breaker import BreakerConfig  # noqa: E402
from considerate.browser import AsyncConsiderateBrowserPage, ConsiderateBrowserPage  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    status_for_path = {}
    robots_txt = "User-agent: *\n"
    hit_count = {"n": 0}

    def do_GET(self):
        _Handler.hit_count["n"] += 1
        if self.path == "/robots.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(_Handler.robots_txt.encode())
            return
        if self.path == "/.well-known/considerate.json":
            self.send_response(404)
            self.end_headers()
            return
        status = self.status_for_path.get(self.path, 200)
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if status < 400:
            self.wfile.write(b"<html><body>ok</body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    _Handler.status_for_path = {}
    _Handler.robots_txt = "User-agent: *\n"
    _Handler.hit_count = {"n": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def sync_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


def test_goto_returns_real_response_and_tracks_domain(local_server, sync_page):
    port = local_server.server_address[1]
    considerate_page = ConsiderateBrowserPage(sync_page, identity=AgentIdentity(name="BrowserTestBot"))

    response = considerate_page.goto(f"http://127.0.0.1:{port}/page")

    assert response.status == 200
    assert sync_page.content().count("ok") == 1
    snap = considerate_page.snapshot()
    assert "127.0.0.1" in snap
    assert snap["127.0.0.1"]["circuit_state"] == "closed"
    considerate_page.close()


def test_circuit_opens_after_repeated_navigation_failures(local_server, sync_page):
    port = local_server.server_address[1]
    _Handler.status_for_path["/broken"] = 503

    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=2, cooldown_seconds=30))
    considerate_page = ConsiderateBrowserPage(sync_page, config=config)

    considerate_page.goto(f"http://127.0.0.1:{port}/broken")
    considerate_page.goto(f"http://127.0.0.1:{port}/broken")

    with pytest.raises(CircuitOpenError) as excinfo:
        considerate_page.goto(f"http://127.0.0.1:{port}/broken")
    assert excinfo.value.payload["status"] == "circuit_open"
    considerate_page.close()


def test_disallow_paths_blocks_navigation_before_it_happens(local_server, sync_page):
    # The considerate.json/robots.txt *fetch* step is exercised for the
    # httpx client elsewhere (test_client.py, test_hardening.py) against a
    # mock transport; here we're specifically proving goto() enforces an
    # already-known policy correctly, so the policy is injected directly
    # rather than routed through a real HTTPS fetch a local dev server on a
    # random port can't receive (considerate always checks /.well-known and
    # robots.txt over https on the standard port, by design — see
    # client.py's _fetch_meta).
    from considerate.policy import SitePolicy

    port = local_server.server_address[1]
    considerate_page = ConsiderateBrowserPage(sync_page)

    state = considerate_page._get_domain("127.0.0.1")
    state.apply_policy(SitePolicy(disallow_paths=["/secret"]))

    hits_before = _Handler.hit_count["n"]
    with pytest.raises(DisallowedError):
        considerate_page.goto(f"http://127.0.0.1:{port}/secret/data")

    assert _Handler.hit_count["n"] == hits_before  # the browser never navigated there
    considerate_page.close()


def test_async_browser_page_goto(local_server):
    # Run in a fresh subprocess rather than in-process: Playwright's async
    # driver bootstrap conflicts with pytest-asyncio's event-loop
    # management as soon as another async test has already run earlier in
    # the same session (this exact scenario passes reliably stand-alone,
    # and fails intermittently in-process depending on test order/pytest-
    # asyncio version — see _async_browser_subprocess.py's docstring). A
    # subprocess sidesteps that entirely instead of chasing a version pin.
    port = local_server.server_address[1]
    script = Path(__file__).parent / "_async_browser_subprocess.py"
    result = subprocess.run(
        [sys.executable, str(script), str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "ASYNC_BROWSER_TEST_OK" in result.stdout
