import json

import httpx
import pytest

from considerate import (
    AgentIdentity,
    AsyncConsiderateClient,
    CircuitOpenError,
    ConsiderateClient,
    ConsiderateConfig,
    DisallowedError,
)
from considerate.breaker import BreakerConfig
from considerate.identity import parse_header

HOST = "testserver"
BASE = f"https://{HOST}"


def _not_found(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def make_handler(*, well_known=None, robots=None, page_status=200, page_body=b"ok"):
    """Build a MockTransport handler.

    `well_known` and `robots` are raw text bodies (or None -> 404). Any
    other path returns `page_status`/`page_body`, and pages under /raise/
    can be used to test disallow handling.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/considerate.json":
            if well_known is None:
                return httpx.Response(404)
            return httpx.Response(200, text=well_known)
        if path == "/robots.txt":
            if robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=robots)
        return httpx.Response(page_status, content=page_body, headers={})

    return handler


def test_sends_identity_header_and_returns_response():
    identity = AgentIdentity(name="TestBot", version="9", contact="mailto:a@b.com")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
            return httpx.Response(404)
        seen["header"] = request.headers.get("considerate-agent")
        return httpx.Response(200, content=b"ok")

    client = ConsiderateClient(identity=identity, transport=httpx.MockTransport(handler))
    response = client.get(f"{BASE}/page")
    assert response.status_code == 200
    parsed = parse_header(seen["header"])
    assert parsed["name"] == "TestBot"
    assert parsed["contact"] == "mailto:a@b.com"


def test_well_known_policy_sets_hard_ceiling():
    well_known = json.dumps({"default": {"requests_per_second": 0.5, "max_concurrent": 1}})
    client = ConsiderateClient(transport=httpx.MockTransport(make_handler(well_known=well_known)))
    client.get(f"{BASE}/page")
    state = client._domains[HOST]
    assert state.hard_ceiling is True
    assert state.controller.rate == 0.5
    assert state.controller.config.max_rate == 0.5


def test_per_agent_override_in_well_known_wins_over_default():
    well_known = json.dumps(
        {
            "default": {"requests_per_second": 0.2},
            "agents": {"TrustedBot": {"requests_per_second": 5.0}},
        }
    )
    identity = AgentIdentity(name="TrustedBot")
    client = ConsiderateClient(identity=identity, transport=httpx.MockTransport(make_handler(well_known=well_known)))
    client.get(f"{BASE}/page")
    assert client._domains[HOST].controller.rate == 5.0


def test_robots_crawl_delay_used_when_no_well_known():
    robots = "User-agent: *\nCrawl-delay: 4\n"
    client = ConsiderateClient(transport=httpx.MockTransport(make_handler(robots=robots)))
    client.get(f"{BASE}/page")
    state = client._domains[HOST]
    assert state.controller.rate == pytest.approx(0.25)


def test_robots_disallow_blocks_request_before_it_is_sent():
    robots = "User-agent: *\nDisallow: /secret\n"
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.path == "/.well-known/considerate.json":
            return httpx.Response(404)
        return httpx.Response(200, content=b"should not be reached")

    client = ConsiderateClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedError):
        client.get(f"{BASE}/secret/data")
    assert "/secret/data" not in calls


def test_respect_robots_txt_false_bypasses_disallow():
    robots = "User-agent: *\nDisallow: /secret\n"
    client = ConsiderateClient(
        config=ConsiderateConfig(respect_robots_txt=False),
        transport=httpx.MockTransport(make_handler(robots=robots)),
    )
    response = client.get(f"{BASE}/secret/data")
    assert response.status_code == 200


def test_circuit_opens_after_consecutive_failures_and_raises_structured_error():
    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=2, cooldown_seconds=30))
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(make_handler(page_status=503)))

    client.get(f"{BASE}/page")  # failure 1
    client.get(f"{BASE}/page")  # failure 2 -> opens

    with pytest.raises(CircuitOpenError) as excinfo:
        client.get(f"{BASE}/page")
    assert excinfo.value.payload["status"] == "circuit_open"
    assert excinfo.value.payload["domain"] == HOST
    assert excinfo.value.payload["retry_after"] > 0


def test_on_event_fires_for_policy_discovery_and_circuit_open():
    events = []
    well_known = json.dumps({"default": {"requests_per_second": 1.0}})
    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=1))
    client = ConsiderateClient(
        config=config,
        on_event=events.append,
        transport=httpx.MockTransport(make_handler(well_known=well_known, page_status=500)),
    )
    client.get(f"{BASE}/page")  # discovers policy, then fails -> opens circuit (threshold=1)
    kinds = [e.kind for e in events]
    assert "policy_discovered" in kinds
    assert "rate_decreased" in kinds


def test_context_manager_closes_underlying_httpx_client():
    with ConsiderateClient(transport=httpx.MockTransport(make_handler())) as client:
        client.get(f"{BASE}/page")
    assert client._httpx.is_closed


@pytest.mark.asyncio
async def test_async_client_basic_roundtrip():
    identity = AgentIdentity(name="AsyncBot")
    async with AsyncConsiderateClient(
        identity=identity, transport=httpx.MockTransport(make_handler())
    ) as client:
        response = await client.get(f"{BASE}/page")
        assert response.status_code == 200
        assert HOST in client._domains


@pytest.mark.asyncio
async def test_async_client_respects_well_known_policy():
    well_known = json.dumps({"default": {"requests_per_second": 0.3}})
    async with AsyncConsiderateClient(
        transport=httpx.MockTransport(make_handler(well_known=well_known))
    ) as client:
        await client.get(f"{BASE}/page")
        assert client._domains[HOST].controller.rate == 0.3
