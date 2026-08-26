"""Tests for client.snapshot() (C3): the structured, out-of-band view of
what considerate currently sees for every domain it's touched.
"""

import json

import httpx
import pytest

from considerate import AgentIdentity, AsyncConsiderateClient, ConsiderateClient


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
        return httpx.Response(404)
    return httpx.Response(200, content=b"ok")


def test_snapshot_empty_before_any_requests():
    client = ConsiderateClient(transport=httpx.MockTransport(_handler))
    assert client.snapshot() == {}


def test_snapshot_reflects_touched_domains():
    client = ConsiderateClient(transport=httpx.MockTransport(_handler))
    client.get("https://snap1.test/page")
    client.get("https://snap2.test/page")

    snap = client.snapshot()
    assert set(snap.keys()) == {"snap1.test", "snap2.test"}
    for domain_stats in snap.values():
        assert domain_stats["circuit_state"] == "closed"
        assert domain_stats["current_rate_req_per_s"] > 0
        assert domain_stats["policy_source"] is None  # nothing published for these mock hosts


def test_snapshot_is_json_serializable():
    client = ConsiderateClient(transport=httpx.MockTransport(_handler))
    client.get("https://snapjson.test/page")
    json.dumps(client.snapshot())  # must not raise


def test_snapshot_shows_declared_rate_for_identity():
    well_known = '{"default": {"requests_per_second": 0.5}, "agents": {"SnapBot": {"requests_per_second": 3.0}}}'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/considerate.json":
            return httpx.Response(200, text=well_known)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"ok")

    client = ConsiderateClient(identity=AgentIdentity(name="SnapBot"), transport=httpx.MockTransport(handler))
    client.get("https://snapdeclared.test/page")
    snap = client.snapshot()["snapdeclared.test"]
    assert snap["declared_rate_for_identity"] == 3.0
    assert snap["policy_source"] == "well-known"
    assert snap["hard_ceiling"] is True


@pytest.mark.asyncio
async def test_async_client_snapshot():
    async with AsyncConsiderateClient(transport=httpx.MockTransport(_handler)) as client:
        await client.get("https://asyncsnap.test/page")
        snap = client.snapshot()
        assert "asyncsnap.test" in snap
