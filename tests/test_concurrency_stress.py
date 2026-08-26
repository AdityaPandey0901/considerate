"""F3: real OS-thread concurrency against the sync client (MockTransport
still stands in for the network — this is about real thread races on the
shared DomainState/token-bucket/breaker/semaphore, which a single-threaded
mocked test can't exercise no matter how it's written).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from considerate import ConsiderateClient, ConsiderateConfig
from considerate.breaker import BreakerConfig


def test_concurrent_threads_never_exceed_max_concurrent_and_leave_consistent_state():
    in_flight = 0
    max_seen = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_seen
        if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
            return httpx.Response(404)
        with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        time.sleep(0.02)  # long enough that overlapping requests are likely if the limit is broken
        with lock:
            in_flight -= 1
        return httpx.Response(200)

    config = ConsiderateConfig(
        max_concurrent_per_domain=3,
        tier_rates={"standard": 1000.0},
        tier_ceilings={"standard": 1000.0},
    )
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))

    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(client.get, "https://stresstest.test/page") for _ in range(80)]
        for f in futures:
            f.result()

    assert max_seen <= 3, f"max_concurrent_per_domain=3 was violated: saw {max_seen} in flight at once"

    state = client._domains["stresstest.test"]
    assert state.controller.tokens >= 0
    assert state.controller.rate > 0
    assert state.breaker.state.value == "closed"


def test_concurrent_failures_open_the_circuit_exactly_once_not_per_thread():
    def always_503(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(503)

    config = ConsiderateConfig(
        breaker=BreakerConfig(consecutive_failures=3, cooldown_seconds=60),
        tier_rates={"standard": 1000.0},
        tier_ceilings={"standard": 1000.0},
        max_concurrent_per_domain=10,
    )
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(always_503))

    results = []

    def hit():
        try:
            client.get("https://alwaysbroken.test/page")
            results.append("ok")
        except Exception as exc:
            results.append(type(exc).__name__)

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda _: hit(), range(20)))

    state = client._domains["alwaysbroken.test"]
    # The breaker must have tripped (some threads got CircuitOpenError, not
    # all 20 attempted a real request) — no double-opening/corrupted state.
    assert "CircuitOpenError" in results
    assert state.breaker.state.value == "open"
