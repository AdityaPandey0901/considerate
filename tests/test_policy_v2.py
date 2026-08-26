"""Tests for the v0.2 policy fields: disallow_paths enforcement,
crawl_windows time-based multipliers, and verified_identity precedence.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from considerate import AsyncConsiderateClient, ConsiderateClient, DisallowedError
from considerate.policy import CrawlWindow, SitePolicy, parse_well_known


def _handler(well_known=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/considerate.json":
            return httpx.Response(200, text=well_known) if well_known else httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"ok")

    return handler


# --- disallow_paths ---------------------------------------------------------


def test_disallow_paths_parsed():
    policy = parse_well_known(json.dumps({"disallow_paths": ["/admin", "/checkout"]}))
    assert policy.is_path_disallowed("/admin/users")
    assert not policy.is_path_disallowed("/products")


def test_client_enforces_policy_disallow_paths():
    well_known = json.dumps({"default": {"requests_per_second": 5}, "disallow_paths": ["/admin"]})
    client = ConsiderateClient(transport=httpx.MockTransport(_handler(well_known)))
    with pytest.raises(DisallowedError):
        client.get("https://policyv2.test/admin/panel")
    # A non-disallowed path on the same domain still works.
    response = client.get("https://policyv2.test/home")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_async_client_enforces_policy_disallow_paths():
    well_known = json.dumps({"disallow_paths": ["/secret"]})
    async with AsyncConsiderateClient(transport=httpx.MockTransport(_handler(well_known))) as client:
        with pytest.raises(DisallowedError):
            await client.get("https://policyv2async.test/secret/data")


# --- crawl_windows -----------------------------------------------------------


def test_crawl_window_multiplier_active_by_day_and_hour():
    window = CrawlWindow(days=("sat", "sun"), hours="00:00-06:00", multiplier=3.0)
    saturday_3am = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)  # a Saturday
    monday_3am = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)  # a Monday
    saturday_noon = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    assert window.is_active(saturday_3am) is True
    assert window.is_active(monday_3am) is False  # wrong day
    assert window.is_active(saturday_noon) is False  # right day, wrong hour


def test_crawl_window_handles_midnight_wraparound():
    window = CrawlWindow(hours="22:00-06:00", multiplier=2.0)
    late_night = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
    early_morning = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    afternoon = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)

    assert window.is_active(late_night) is True
    assert window.is_active(early_morning) is True
    assert window.is_active(afternoon) is False


def test_active_multiplier_picks_the_largest_of_overlapping_windows():
    policy = SitePolicy(crawl_windows=[CrawlWindow(multiplier=2.0), CrawlWindow(multiplier=5.0)])
    assert policy.active_multiplier(datetime.now(timezone.utc)) == 5.0


def test_active_multiplier_defaults_to_one_with_no_windows():
    assert SitePolicy().active_multiplier() == 1.0


def test_client_applies_crawl_window_ceiling_multiplier():
    # A window that is always active (no days/hours restriction) at 10x a
    # tiny base rate — the effective ceiling should reflect the multiplier.
    well_known = json.dumps(
        {
            "default": {"requests_per_second": 0.5},
            "crawl_windows": [{"multiplier": 10}],
        }
    )
    client = ConsiderateClient(transport=httpx.MockTransport(_handler(well_known)))
    client.get("https://windowtest.test/page")
    state = client._domains["windowtest.test"]
    assert state.base_ceiling == 0.5
    assert state.controller.config.max_rate == 5.0  # 0.5 * 10x window multiplier


# --- verified_identity precedence -------------------------------------------


def test_verified_identity_outranks_self_declared_name():
    well_known = json.dumps(
        {
            "default": {"requests_per_second": 0.2},
            "agents": {"SelfDeclaredName": {"requests_per_second": 1.0}},
            "verified_agents": {"https://issuer.example/bots/trusted-bot": {"requests_per_second": 9.0}},
        }
    )
    from considerate import AgentIdentity

    identity = AgentIdentity(name="SelfDeclaredName")
    client = ConsiderateClient(
        identity=identity,
        verified_identity="https://issuer.example/bots/trusted-bot",
        transport=httpx.MockTransport(_handler(well_known)),
    )
    client.get("https://verifiedtest.test/page")
    assert client._domains["verifiedtest.test"].controller.rate == 9.0
