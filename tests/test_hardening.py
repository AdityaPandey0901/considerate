"""Tests for the A1-A6 correctness fixes: async concurrency, cross-domain
redirects, Retry-After as a hard floor, domain-tracking LRU eviction,
oversized meta-fetch responses, and host case normalization.
"""

import asyncio
import time

import httpx
import pytest

from considerate import AsyncConsiderateClient, ConsiderateClient, ConsiderateConfig
from considerate.config import TIER_DEFAULTS


def _meta_404(request: httpx.Request) -> httpx.Response | None:
    if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
        return httpx.Response(404)
    return None


# --- A1: async concurrency limit uses a real asyncio.Semaphore -------------


@pytest.mark.asyncio
async def test_async_client_respects_max_concurrent_without_blocking_loop():
    in_flight = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_seen
        meta = _meta_404(request)
        if meta is not None:
            return meta
        async with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return httpx.Response(200)

    # A heartbeat task proves the event loop stays responsive while requests
    # are "blocked" waiting on the semaphore — a threading.Semaphore used
    # from async code would starve this.
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    config = ConsiderateConfig(
        max_concurrent_per_domain=2, tier_rates={"standard": 1000.0}, tier_ceilings={"standard": 1000.0}
    )
    async with AsyncConsiderateClient(config=config, transport=httpx.MockTransport(handler)) as client:
        hb = asyncio.create_task(heartbeat())
        await asyncio.gather(*[client.get("https://concurrency.test/page") for _ in range(6)])
        await hb

    assert max_seen <= 2
    assert ticks == 20  # heartbeat ran to completion concurrently, loop never starved


# --- A2: redirects are followed through considerate's own metering --------


def test_redirect_crosses_domain_and_gets_its_own_state():
    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        if meta is not None:
            return meta
        if request.url.host == "a.redirecttest":
            return httpx.Response(302, headers={"location": "https://b.redirecttest/dest"})
        if request.url.host == "b.redirecttest":
            return httpx.Response(200, content=b"landed")
        return httpx.Response(404)

    client = ConsiderateClient(transport=httpx.MockTransport(handler))
    response = client.get("https://a.redirecttest/start")

    assert response.status_code == 200
    assert response.content == b"landed"
    assert "a.redirecttest" in client._domains
    assert "b.redirecttest" in client._domains  # second hop got its own policy discovery + state


def test_post_303_downgrades_to_get_and_drops_body():
    seen_methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        if meta is not None:
            return meta
        seen_methods.append(request.method)
        if request.url.host == "form.redirecttest":
            return httpx.Response(303, headers={"location": "https://after.redirecttest/done"})
        return httpx.Response(200)

    client = ConsiderateClient(transport=httpx.MockTransport(handler))
    client.request("POST", "https://form.redirecttest/submit", json={"x": 1})

    assert seen_methods == ["POST", "GET"]


def test_follow_redirects_false_returns_redirect_response_untouched():
    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        if meta is not None:
            return meta
        return httpx.Response(302, headers={"location": "https://elsewhere.redirecttest/x"})

    client = ConsiderateClient(transport=httpx.MockTransport(handler))
    response = client.get("https://noredirect.redirecttest/start", follow_redirects=False)
    assert response.status_code == 302
    assert "elsewhere.redirecttest" not in client._domains


# --- A3: Retry-After is a hard floor, not just a controller signal ---------


def test_retry_after_delays_the_next_request_even_after_breaker_would_allow_it():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        if meta is not None:
            return meta
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200)

    config = ConsiderateConfig(tier_rates={"standard": 1000.0}, tier_ceilings={"standard": 1000.0})
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))

    client.get("https://retryafter.test/page")  # 429, sets a 1s floor
    start = time.monotonic()
    client.get("https://retryafter.test/page")  # must wait for the floor despite a fast token bucket
    elapsed = time.monotonic() - start

    assert elapsed >= 0.9


# --- A4: LRU eviction bounds memory for long-running, many-domain agents ---


def test_domain_tracking_evicts_coldest_past_the_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        return meta if meta is not None else httpx.Response(200)

    config = ConsiderateConfig(
        max_tracked_domains=3, tier_rates={"standard": 1000.0}, tier_ceilings={"standard": 1000.0}
    )
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))

    for i in range(5):
        client.get(f"https://host{i}.lrutest/page")

    assert len(client._domains) == 3
    assert "host0.lrutest" not in client._domains  # coldest, evicted first
    assert "host4.lrutest" in client._domains  # most recent, survives


# --- A5: an oversized meta-fetch response is treated as "absent" ----------


def test_oversized_well_known_response_is_ignored_not_crashed_on():
    huge = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/considerate.json":
            return httpx.Response(200, content=huge)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200)

    config = ConsiderateConfig(meta_fetch_max_bytes=1000)
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))

    response = client.get("https://oversized.test/page")
    assert response.status_code == 200
    assert client._domains["oversized.test"].policy is None


# --- A6: host matching is case-insensitive (verifying httpx's own normalization) ---


def test_host_case_is_normalized_to_one_domain_state():
    def handler(request: httpx.Request) -> httpx.Response:
        meta = _meta_404(request)
        return meta if meta is not None else httpx.Response(200)

    client = ConsiderateClient(transport=httpx.MockTransport(handler))
    client.get("https://CaseTest.example/page")
    client.get("https://casetest.example/page")

    assert len(client._domains) == 1
    assert TIER_DEFAULTS  # sanity import check that config constants are reachable
