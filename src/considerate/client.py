"""The SafeFetch-style wrapper: a (mostly) drop-in replacement for
`httpx.Client` / `httpx.AsyncClient` that runs every request through the
capacity estimator, AIMD controller, and circuit breaker for its domain.

    from considerate import ConsiderateClient, AgentIdentity

    client = ConsiderateClient(identity=AgentIdentity(name="MyResearchBot", contact="mailto:me@example.com"))
    response = client.get("https://example.com/page")

Async is the same shape:

    async with AsyncConsiderateClient(identity=...) as client:
        response = await client.get("https://example.com/page")
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx

from ._domain import DomainState
from .config import ConsiderateConfig
from .events import EventCallback, emit
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity
from .policy import PolicyError, parse_robots_crawl_delay, parse_well_known

_DEFAULT_IDENTITY = AgentIdentity(name="considerate-agent", version="0", intent="browse")
_META_FETCH_TIMEOUT = 3.0


def _host_of(url: str | httpx.URL) -> str:
    return httpx.URL(url).host


class _PolicyMixin:
    """Shared, I/O-free plumbing between the sync and async clients."""

    def _new_domain_state(self, host: str) -> DomainState:
        return DomainState(host=host, config=self.config, agent_name=self.identity.name)

    def _parse_well_known_response(self, host: str, status_code: int, text: str):
        if status_code != 200:
            return None
        try:
            return parse_well_known(text)
        except PolicyError as exc:
            emit(self.on_event, "policy_error", host, error=str(exc), source="well-known")
            return None

    def _parse_robots_response(self, status_code: int, text: str):
        if status_code != 200:
            return None, None
        return parse_robots_crawl_delay(text, user_agent=self.identity.name)


class ConsiderateClient(_PolicyMixin):
    """Sync client. Wraps `httpx.Client`."""

    def __init__(
        self,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        self._httpx = httpx.Client(**httpx_kwargs)
        self._domains: dict[str, DomainState] = {}
        self._domains_lock = threading.Lock()

    def __enter__(self) -> "ConsiderateClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._httpx.close()

    def _get_domain(self, host: str) -> DomainState:
        with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
                self._domains[host] = state
            return state

    def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        try:
            resp = self._httpx.get(f"https://{host}{path}", timeout=_META_FETCH_TIMEOUT)
            return resp.status_code, resp.text
        except httpx.HTTPError:
            return None

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
                crawl_policy, parser = self._parse_robots_response(*fetched)
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

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = _host_of(url)
        state = self._get_domain(host)

        self._ensure_policy(state, host)

        robots_parser = getattr(state, "robots_parser", None)
        if self.config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)

        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)

        with state.semaphore:
            wait = state.controller.wait_time()
            if wait > 0:
                time.sleep(wait)
            state.controller.consume()

            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Considerate-Agent", self.identity.to_header())
            kwargs["headers"] = headers

            start = time.monotonic()
            try:
                response = self._httpx.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                state.controller.report_failure()
                state.breaker.report_failure(type(exc).__name__)
                raise

            latency = time.monotonic() - start
            self._record_outcome(state, host, response, latency)
            return response

    def _record_outcome(self, state: DomainState, host: str, response: httpx.Response, latency: float) -> None:
        if not state.calibrated:
            state.apply_inference(latency, response.headers)

        if response.status_code in (429, 503) or response.status_code >= 500:
            state.controller.report_failure()
            state.breaker.report_failure(f"http_{response.status_code}")
            emit(self.on_event, "rate_decreased", host, status=response.status_code, new_rate=state.controller.rate)
        else:
            before = state.controller.rate
            state.controller.report_success(latency)
            state.breaker.report_success()
            if state.controller.rate != before:
                emit(self.on_event, "rate_increased", host, new_rate=state.controller.rate)

    # Convenience verbs mirroring httpx.Client
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)


class AsyncConsiderateClient(_PolicyMixin):
    """Async client. Wraps `httpx.AsyncClient`."""

    def __init__(
        self,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        self._httpx = httpx.AsyncClient(**httpx_kwargs)
        self._domains: dict[str, DomainState] = {}
        self._domains_lock = asyncio.Lock()

    async def __aenter__(self) -> "AsyncConsiderateClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._httpx.aclose()

    async def _get_domain(self, host: str) -> DomainState:
        async with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
                self._domains[host] = state
            return state

    async def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        try:
            resp = await self._httpx.get(f"https://{host}{path}", timeout=_META_FETCH_TIMEOUT)
            return resp.status_code, resp.text
        except httpx.HTTPError:
            return None

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
                crawl_policy, parser = self._parse_robots_response(*fetched)
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

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = _host_of(url)
        state = await self._get_domain(host)

        await self._ensure_policy(state, host)

        robots_parser = getattr(state, "robots_parser", None)
        if self.config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)

        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)

        # Concurrency is capped with a plain threading.Semaphore even here:
        # it's fast, non-blocking-in-practice at this scale, and lets
        # DomainState stay identical between sync and async paths.
        with state.semaphore:
            wait = state.controller.wait_time()
            if wait > 0:
                await asyncio.sleep(wait)
            state.controller.consume()

            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Considerate-Agent", self.identity.to_header())
            kwargs["headers"] = headers

            start = time.monotonic()
            try:
                response = await self._httpx.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                state.controller.report_failure()
                state.breaker.report_failure(type(exc).__name__)
                raise

            latency = time.monotonic() - start
            self._record_outcome(state, host, response, latency)
            return response

    def _record_outcome(self, state: DomainState, host: str, response: httpx.Response, latency: float) -> None:
        if not state.calibrated:
            state.apply_inference(latency, response.headers)

        if response.status_code in (429, 503) or response.status_code >= 500:
            state.controller.report_failure()
            state.breaker.report_failure(f"http_{response.status_code}")
            emit(self.on_event, "rate_decreased", host, status=response.status_code, new_rate=state.controller.rate)
        else:
            before = state.controller.rate
            state.controller.report_success(latency)
            state.breaker.report_success()
            if state.controller.rate != before:
                emit(self.on_event, "rate_increased", host, new_rate=state.controller.rate)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)
