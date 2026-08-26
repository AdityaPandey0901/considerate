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
from collections import OrderedDict
from typing import Any

import httpx

from ._cache import PersistentPolicyCache, load_fresh
from ._domain import DomainState
from ._redirects import is_redirect, next_hop
from ._types import ResponseLike
from ._util import classify_transport_failure, parse_retry_after
from .config import ConsiderateConfig
from .events import EventCallback, emit
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity
from .policy import PolicyError, parse_robots_crawl_delay, parse_well_known

_DEFAULT_IDENTITY = AgentIdentity(name="considerate-agent", version="0", intent="browse")
_META_FETCH_TIMEOUT = 3.0
_DEGRADED_STATUSES = (429, 503)


def _host_of(url: str | httpx.URL) -> str:
    return httpx.URL(url).host


class _SharedLogic:
    """I/O-free plumbing shared between the sync and async clients: building
    domain state, parsing meta-fetch responses, deciding what a response
    outcome means for the controller/breaker, and LRU bookkeeping. Neither
    method here ever awaits or blocks — that's entirely the callers' job.

    Every concrete subclass (`ConsiderateClient`, `AsyncConsiderateClient`,
    `ConsiderateAdapter`, the browser wrapper classes) sets these in its own
    `__init__` — declared here, without values, purely so a type checker
    knows they exist rather than flagging every reference in this mixin as
    an unknown attribute. `ConsiderateAdapter` is the one exception for
    `config` specifically (see its module for why) — it satisfies this
    Protocol-by-convention with `considerate_config` instead and overrides
    the two methods here that touch `config` directly.
    """

    config: ConsiderateConfig
    identity: AgentIdentity
    on_event: EventCallback | None
    verified_identity: str | None
    _domains: OrderedDict[str, DomainState]
    _persistent_cache: PersistentPolicyCache | None

    def _init_persistent_cache(self) -> None:
        path = self.config.policy_cache_path
        self._persistent_cache = PersistentPolicyCache(path) if path else None

    def _close_persistent_cache(self) -> None:
        if self._persistent_cache is not None:
            self._persistent_cache.close()

    def _new_domain_state(self, host: str) -> DomainState:
        return DomainState(
            host=host,
            config=self.config,
            agent_name=self.identity.name,
            verified_identity=self.verified_identity,
        )

    def _touch_domain(self, host: str, state: DomainState) -> DomainState:
        """Insert/refresh `host` as most-recently-used, evicting the coldest
        tracked domain past `max_tracked_domains` — otherwise an agent that
        touches many distinct hosts over a long run leaks memory forever.
        """
        self._domains[host] = state
        self._domains.move_to_end(host)
        while len(self._domains) > self.config.max_tracked_domains:
            self._domains.popitem(last=False)
        return state

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

    def _domain_snapshot(self, state: DomainState) -> dict[str, Any]:
        rule = state.policy.rule_for(self.identity.name, self.verified_identity) if state.policy else None
        return {
            "domain": state.host,
            "policy_source": state.policy.source if state.policy else None,
            "policy_contact": state.policy.contact if state.policy else None,
            "hard_ceiling": state.hard_ceiling,
            "calibrated": state.calibrated,
            "current_rate_req_per_s": round(state.controller.rate, 4),
            "base_ceiling_req_per_s": round(state.base_ceiling, 4),
            "effective_ceiling_req_per_s": round(state.controller.config.max_rate, 4),
            "max_concurrent": state.max_concurrent,
            "declared_rate_for_identity": rule.requests_per_second if rule else None,
            "circuit_state": state.breaker.state.value,
        }

    def _record_outcome(self, state: DomainState, host: str, response: ResponseLike, latency: float) -> None:
        if not state.calibrated:
            state.apply_inference(latency, response.headers)

        if response.status_code in _DEGRADED_STATUSES:
            retry_seconds = parse_retry_after(response.headers.get("retry-after"))
            if retry_seconds is not None:
                state.note_retry_after(retry_seconds)

        if response.status_code in _DEGRADED_STATUSES or response.status_code >= 500:
            state.controller.report_failure()
            state.breaker.report_failure(f"http_{response.status_code}")
            emit(self.on_event, "rate_decreased", host, status=response.status_code, new_rate=state.controller.rate)
        else:
            before = state.controller.rate
            state.controller.report_success(latency)
            state.breaker.report_success()
            if state.controller.rate != before:
                emit(self.on_event, "rate_increased", host, new_rate=state.controller.rate)


class ConsiderateClient(_SharedLogic):
    """Sync client. Wraps `httpx.Client`."""

    def __init__(
        self,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        verified_identity: str | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        # An identity confirmed by an out-of-band verification step (e.g. a
        # Web Bot Auth signature check performed before considerate ever
        # sees the request) — see SitePolicy.verified_agents / SPEC.md §6.
        # considerate does not verify anything itself; this is purely "the
        # caller already checked, here's what it resolved to."
        self.verified_identity = verified_identity
        self._httpx = httpx.Client(**httpx_kwargs)
        self._domains: OrderedDict[str, DomainState] = OrderedDict()
        self._domains_lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}
        self._init_persistent_cache()

    def __enter__(self) -> ConsiderateClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._httpx.close()
        self._close_persistent_cache()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """A structured dump of every domain this client has touched so
        far — current rate, ceiling, circuit breaker state, discovered
        policy. For a dashboard, a log line, or a health check that wants
        to watch a long-running agent from the outside, without threading
        an `on_event` callback through every call site.
        """
        with self._domains_lock:
            return {host: self._domain_snapshot(state) for host, state in self._domains.items()}

    def _get_domain(self, host: str) -> DomainState:
        with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
            return self._touch_domain(host, state)

    def _get_semaphore(self, state: DomainState) -> threading.Semaphore:
        # Rebuilt whenever the configured limit changes (e.g. a policy fetch
        # updates state.max_concurrent) rather than mutated in place —
        # threading.Semaphore has no public way to change its capacity.
        if self._semaphore_limits.get(state.host) != state.max_concurrent:
            self._semaphores[state.host] = threading.Semaphore(state.max_concurrent)
            self._semaphore_limits[state.host] = state.max_concurrent
        return self._semaphores[state.host]

    def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        url = f"https://{host}{path}"
        try:
            with self._httpx.stream("GET", url, timeout=_META_FETCH_TIMEOUT) as resp:
                if resp.status_code != 200:
                    return resp.status_code, ""
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > self.config.meta_fetch_max_bytes:
                        return None  # oversized — treat exactly like "absent"
                    chunks.append(chunk)
                return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")
        except httpx.HTTPError:
            return None

    def _ensure_policy(self, state: DomainState, host: str) -> None:
        if state.policy_is_fresh():
            return

        well_known_policy = None
        used_disk_cache = False
        if self._persistent_cache is not None:
            disk_policy = load_fresh(self._persistent_cache, host, self.config.policy_cache_ttl)
            if disk_policy is not None:
                well_known_policy = disk_policy
                used_disk_cache = True
        if not used_disk_cache and self.config.fetch_well_known:
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
            if not used_disk_cache and self._persistent_cache is not None and well_known_policy.source == "well-known":
                # Only explicit policy files are worth persisting across
                # process restarts — a crawl-delay-derived one is cheap to
                # re-derive, and robots.txt is fetched fresh every time
                # anyway for its Disallow rules.
                self._persistent_cache.set(host, well_known_policy, time.time())
            label = well_known_policy.source + (" (disk cache)" if used_disk_cache else "")
            emit(self.on_event, "policy_discovered", host, source=label)
        else:
            state.policy_fetched_at = time.monotonic()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", True)
        max_redirects = self.config.max_redirects

        for _ in range(max_redirects + 1):
            response = self._request_once(method, url, **kwargs)
            if not follow_redirects or not is_redirect(response):
                return response
            method, url, kwargs = next_hop(
                method, response.status_code, str(response.url), response.headers["location"], kwargs
            )
            emit(self.on_event, "redirect_followed", _host_of(url), to=url, status=response.status_code)

        return response  # max_redirects exhausted — return the last redirect response as-is

    def _request_once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = _host_of(url)
        state = self._get_domain(host)

        self._ensure_policy(state, host)

        robots_parser = state.robots_parser
        if self.config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)
        if state.is_path_disallowed(httpx.URL(url).path):
            emit(self.on_event, "disallowed", host, url=url, source="considerate.json disallow_paths")
            raise DisallowedError(url)

        state.refresh_effective_ceiling()  # crawl_windows can change the ceiling between requests
        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)

        with self._get_semaphore(state):
            wait = max(state.controller.wait_time(), state.retry_wait_remaining())
            if wait > 0:
                time.sleep(wait)
            state.controller.consume()

            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Considerate-Agent", self.identity.to_header())
            kwargs["headers"] = headers

            start = time.monotonic()
            try:
                response = self._httpx.request(method, url, follow_redirects=False, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                state.controller.report_failure()
                state.breaker.report_failure(classify_transport_failure(exc))
                raise

            latency = time.monotonic() - start
            self._record_outcome(state, host, response, latency)
            return response

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


class AsyncConsiderateClient(_SharedLogic):
    """Async client. Wraps `httpx.AsyncClient`."""

    def __init__(
        self,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        verified_identity: str | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        self.identity = identity or _DEFAULT_IDENTITY
        self.config = config or ConsiderateConfig()
        self.on_event = on_event
        self.verified_identity = verified_identity
        self._httpx = httpx.AsyncClient(**httpx_kwargs)
        self._domains: OrderedDict[str, DomainState] = OrderedDict()
        self._domains_lock = asyncio.Lock()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}
        self._init_persistent_cache()  # sqlite I/O is fast enough locally to do synchronously here

    async def __aenter__(self) -> AsyncConsiderateClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._httpx.aclose()
        self._close_persistent_cache()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Same as `ConsiderateClient.snapshot()`. Deliberately sync, not
        async: it only reads already-computed state, no I/O — and requiring
        an `await` for a monitoring read would be an odd asymmetry with
        `on_event`, which is also plain, sync callbacks. Not lock-protected
        (unlike the sync client) since that would need `await`; a snapshot
        racing a concurrent `_get_domain` insert is acceptable for a
        best-effort read, not a correctness-sensitive one.
        """
        return {host: self._domain_snapshot(state) for host, state in self._domains.items()}

    async def _get_domain(self, host: str) -> DomainState:
        async with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
            return self._touch_domain(host, state)

    def _get_semaphore(self, state: DomainState) -> asyncio.Semaphore:
        if self._semaphore_limits.get(state.host) != state.max_concurrent:
            self._semaphores[state.host] = asyncio.Semaphore(state.max_concurrent)
            self._semaphore_limits[state.host] = state.max_concurrent
        return self._semaphores[state.host]

    async def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        url = f"https://{host}{path}"
        try:
            async with self._httpx.stream("GET", url, timeout=_META_FETCH_TIMEOUT) as resp:
                if resp.status_code != 200:
                    return resp.status_code, ""
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > self.config.meta_fetch_max_bytes:
                        return None
                    chunks.append(chunk)
                return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")
        except httpx.HTTPError:
            return None

    async def _ensure_policy(self, state: DomainState, host: str) -> None:
        if state.policy_is_fresh():
            return

        well_known_policy = None
        used_disk_cache = False
        if self._persistent_cache is not None:
            disk_policy = load_fresh(self._persistent_cache, host, self.config.policy_cache_ttl)
            if disk_policy is not None:
                well_known_policy = disk_policy
                used_disk_cache = True
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
            if not used_disk_cache and self._persistent_cache is not None and well_known_policy.source == "well-known":
                # Only explicit policy files are worth persisting across
                # process restarts — a crawl-delay-derived one is cheap to
                # re-derive, and robots.txt is fetched fresh every time
                # anyway for its Disallow rules.
                self._persistent_cache.set(host, well_known_policy, time.time())
            label = well_known_policy.source + (" (disk cache)" if used_disk_cache else "")
            emit(self.on_event, "policy_discovered", host, source=label)
        else:
            state.policy_fetched_at = time.monotonic()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", True)
        max_redirects = self.config.max_redirects

        for _ in range(max_redirects + 1):
            response = await self._request_once(method, url, **kwargs)
            if not follow_redirects or not is_redirect(response):
                return response
            method, url, kwargs = next_hop(
                method, response.status_code, str(response.url), response.headers["location"], kwargs
            )
            emit(self.on_event, "redirect_followed", _host_of(url), to=url, status=response.status_code)

        return response

    async def _request_once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = _host_of(url)
        state = await self._get_domain(host)

        await self._ensure_policy(state, host)

        robots_parser = state.robots_parser
        if self.config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)
        if state.is_path_disallowed(httpx.URL(url).path):
            emit(self.on_event, "disallowed", host, url=url, source="considerate.json disallow_paths")
            raise DisallowedError(url)

        state.refresh_effective_ceiling()  # crawl_windows can change the ceiling between requests
        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)

        async with self._get_semaphore(state):
            wait = max(state.controller.wait_time(), state.retry_wait_remaining())
            if wait > 0:
                await asyncio.sleep(wait)
            state.controller.consume()

            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Considerate-Agent", self.identity.to_header())
            kwargs["headers"] = headers

            start = time.monotonic()
            try:
                response = await self._httpx.request(method, url, follow_redirects=False, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                state.controller.report_failure()
                state.breaker.report_failure(classify_transport_failure(exc))
                raise

            latency = time.monotonic() - start
            self._record_outcome(state, host, response, latency)
            return response

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
