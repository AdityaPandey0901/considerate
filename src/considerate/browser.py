"""Throttle real browser navigations, the same way `ConsiderateClient`
throttles HTTP requests — for browser agents (browser-use, Stagehand, raw
Playwright) that drive an actual browser rather than making HTTP calls
directly. This is the gap the httpx- and requests-based integrations don't
cover: a browser agent's traffic never goes through either of them.

    from playwright.sync_api import sync_playwright
    from considerate import AgentIdentity
    from considerate.browser import ConsiderateBrowserPage

    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        considerate_page = ConsiderateBrowserPage(page, identity=AgentIdentity(name="MyBrowserAgent"))
        considerate_page.goto("https://example.com/page-1")
        considerate_page.goto("https://example.com/page-2")

Async is the same shape, wrapping `playwright.async_api.Page`:

    considerate_page = AsyncConsiderateBrowserPage(page, identity=...)
    await considerate_page.goto("https://example.com/page-1")

Scope, deliberately: this meters *navigations* (`goto`), not every
sub-resource request a page triggers (images, scripts, XHRs) — matching the
rest of this library's "one request per page visited" model, and avoiding
the very different (and much more invasive) design of routing every network
event through considerate. It also doesn't re-meter individual redirect
hops the way `ConsiderateClient` does (see `_redirects.py`): Playwright
resolves a navigation's redirect chain internally and returns one
`Response` for wherever it ended up.

Requires the optional `playwright` dependency: `pip install considerate[playwright]`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx

try:
    import playwright  # noqa: F401  (import only to produce a clear error if missing)
except ImportError as exc:  # pragma: no cover - exercised via the error message
    raise ImportError(
        "considerate.browser requires the 'playwright' package: pip install considerate[playwright]"
    ) from exc

from ._domain import DomainState
from .client import _SharedLogic
from .config import ConsiderateConfig
from .events import EventCallback, emit
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity
from .policy import parse_robots_crawl_delay

_DEFAULT_IDENTITY = AgentIdentity(name="considerate-browser-agent", version="0", intent="browse")
_META_FETCH_TIMEOUT = 3.0


class _PlaywrightResponseAdapter:
    """Bridges a Playwright `Response` (`.status` / `.headers`) into the
    `.status_code` / `.headers` shape `_SharedLogic._record_outcome` and
    `DomainState.apply_inference` expect from an httpx-style response —
    letting the browser wrapper reuse that logic unmodified.
    """

    def __init__(self, response: Any) -> None:
        self.status_code = response.status
        self.headers = response.headers  # dict[str, str], already lowercase-keyed


class _BrowserSharedLogic(_SharedLogic):
    """The policy/robots parsing and disallow/breaker checks are identical
    between sync and async — only how `_fetch_meta` and `goto` actually do
    I/O differs. Shared here to avoid a second copy of that decision logic.
    """

    def _check_disallowed(self, state: DomainState, host: str, url: str) -> None:
        robots_parser = state.robots_parser
        if self.config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)
        if state.is_path_disallowed(httpx.URL(url).path):
            emit(self.on_event, "disallowed", host, url=url, source="considerate.json disallow_paths")
            raise DisallowedError(url)

    def _check_circuit(self, state: DomainState, host: str) -> None:
        state.refresh_effective_ceiling()
        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)


class ConsiderateBrowserPage(_BrowserSharedLogic):
    """Wraps a `playwright.sync_api.Page`."""

    def __init__(
        self,
        page: Any,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        verified_identity: str | None = None,
    ) -> None:
        self.page = page
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        self.verified_identity = verified_identity
        self._domains: OrderedDict[str, DomainState] = OrderedDict()
        self._domains_lock = threading.Lock()
        self._meta_client = httpx.Client()

    def close(self) -> None:
        self._meta_client.close()

    def __enter__(self) -> ConsiderateBrowserPage:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        try:
            resp = self._meta_client.get(f"https://{host}{path}", timeout=_META_FETCH_TIMEOUT)
        except httpx.HTTPError:
            return None
        if len(resp.content) > self.config.meta_fetch_max_bytes:
            return None
        return resp.status_code, resp.text

    def _ensure_policy(self, state: DomainState, host: str) -> None:
        if state.policy_is_fresh():
            return
        well_known_policy = None
        if self.config.fetch_well_known:
            fetched = self._fetch_meta(host, "/.well-known/considerate.json")
            if fetched:
                well_known_policy = self._parse_well_known_response(host, *fetched)
        if self.config.respect_robots_txt:
            fetched = self._fetch_meta(host, "/robots.txt")
            if fetched:
                crawl_policy, parser = parse_robots_crawl_delay(fetched[1], user_agent=self.identity.name)
                state.robots_parser = parser
                if well_known_policy is None:
                    well_known_policy = crawl_policy
            else:
                state.robots_parser = None
        if well_known_policy is not None:
            state.apply_policy(well_known_policy)
            emit(self.on_event, "policy_discovered", host, source=well_known_policy.source)
        else:
            state.policy_fetched_at = time.monotonic()

    def _get_domain(self, host: str) -> DomainState:
        with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
            return self._touch_domain(host, state)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._domains_lock:
            return {host: self._domain_snapshot(state) for host, state in self._domains.items()}

    def goto(self, url: str, **kwargs: Any) -> Any:
        host = httpx.URL(url).host
        state = self._get_domain(host)
        self._ensure_policy(state, host)
        self._check_disallowed(state, host, url)
        self._check_circuit(state, host)

        wait = max(state.controller.wait_time(), state.retry_wait_remaining())
        if wait > 0:
            time.sleep(wait)
        state.controller.consume()

        start = time.monotonic()
        try:
            response = self.page.goto(url, **kwargs)
        except Exception:  # Playwright's own Error type — see module docstring on scope
            state.controller.report_failure()
            state.breaker.report_failure("navigation_error")
            raise
        latency = time.monotonic() - start

        if response is not None:
            self._record_outcome(state, host, _PlaywrightResponseAdapter(response), latency)
        return response


class AsyncConsiderateBrowserPage(_BrowserSharedLogic):
    """Wraps a `playwright.async_api.Page`."""

    def __init__(
        self,
        page: Any,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        verified_identity: str | None = None,
    ) -> None:
        self.page = page
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        self.verified_identity = verified_identity
        self._domains: OrderedDict[str, DomainState] = OrderedDict()
        self._domains_lock = asyncio.Lock()
        self._meta_client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._meta_client.aclose()

    async def __aenter__(self) -> AsyncConsiderateBrowserPage:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        try:
            resp = await self._meta_client.get(f"https://{host}{path}", timeout=_META_FETCH_TIMEOUT)
        except httpx.HTTPError:
            return None
        if len(resp.content) > self.config.meta_fetch_max_bytes:
            return None
        return resp.status_code, resp.text

    async def _ensure_policy(self, state: DomainState, host: str) -> None:
        if state.policy_is_fresh():
            return
        well_known_policy = None
        if self.config.fetch_well_known:
            fetched = await self._fetch_meta(host, "/.well-known/considerate.json")
            if fetched:
                well_known_policy = self._parse_well_known_response(host, *fetched)
        if self.config.respect_robots_txt:
            fetched = await self._fetch_meta(host, "/robots.txt")
            if fetched:
                crawl_policy, parser = parse_robots_crawl_delay(fetched[1], user_agent=self.identity.name)
                state.robots_parser = parser
                if well_known_policy is None:
                    well_known_policy = crawl_policy
            else:
                state.robots_parser = None
        if well_known_policy is not None:
            state.apply_policy(well_known_policy)
            emit(self.on_event, "policy_discovered", host, source=well_known_policy.source)
        else:
            state.policy_fetched_at = time.monotonic()

    async def _get_domain(self, host: str) -> DomainState:
        async with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
            return self._touch_domain(host, state)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {host: self._domain_snapshot(state) for host, state in self._domains.items()}

    async def goto(self, url: str, **kwargs: Any) -> Any:
        host = httpx.URL(url).host
        state = await self._get_domain(host)
        await self._ensure_policy(state, host)
        self._check_disallowed(state, host, url)
        self._check_circuit(state, host)

        wait = max(state.controller.wait_time(), state.retry_wait_remaining())
        if wait > 0:
            await asyncio.sleep(wait)
        state.controller.consume()

        start = time.monotonic()
        try:
            response = await self.page.goto(url, **kwargs)
        except Exception:
            state.controller.report_failure()
            state.breaker.report_failure("navigation_error")
            raise
        latency = time.monotonic() - start

        if response is not None:
            self._record_outcome(state, host, _PlaywrightResponseAdapter(response), latency)
        return response
