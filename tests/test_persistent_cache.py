"""Tests for the on-disk policy cache (C2): the sqlite layer directly, and
end-to-end across two separate ConsiderateClient instances simulating a
process restart.
"""

import json
import time

import httpx
import pytest

from considerate import ConsiderateClient, ConsiderateConfig
from considerate._cache import PersistentPolicyCache, load_fresh
from considerate.policy import RateRule, SitePolicy


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "policy_cache.sqlite3")


def test_set_and_get_roundtrip(db_path):
    cache = PersistentPolicyCache(db_path)
    policy = SitePolicy(contact="mailto:a@b.com", default=RateRule(requests_per_second=1.5), source="well-known")
    cache.set("example.com", policy, fetched_at=time.time())

    loaded = cache.get("example.com")
    assert loaded is not None
    loaded_policy, fetched_at = loaded
    assert loaded_policy.contact == "mailto:a@b.com"
    assert loaded_policy.default.requests_per_second == 1.5
    assert loaded_policy.source == "well-known"
    cache.close()


def test_get_missing_host_returns_none(db_path):
    cache = PersistentPolicyCache(db_path)
    assert cache.get("nowhere.test") is None
    cache.close()


def test_survives_reopening_the_same_file(db_path):
    policy = SitePolicy(default=RateRule(requests_per_second=2.0), source="well-known")
    cache = PersistentPolicyCache(db_path)
    cache.set("persist.test", policy, fetched_at=time.time())
    cache.close()

    reopened = PersistentPolicyCache(db_path)
    loaded = reopened.get("persist.test")
    assert loaded is not None
    assert loaded[0].default.requests_per_second == 2.0
    reopened.close()


def test_load_fresh_respects_ttl(db_path):
    cache = PersistentPolicyCache(db_path)
    policy = SitePolicy(default=RateRule(requests_per_second=1.0), source="well-known")
    stale_timestamp = time.time() - 1000
    cache.set("stale.test", policy, fetched_at=stale_timestamp)

    assert load_fresh(cache, "stale.test", ttl=500) is None  # older than ttl
    assert load_fresh(cache, "stale.test", ttl=2000) is not None  # within ttl
    cache.close()


def test_corrupted_row_is_treated_as_a_cache_miss(db_path):
    cache = PersistentPolicyCache(db_path)
    cache._conn.execute(
        "INSERT INTO policies (host, fetched_at, source, data) VALUES (?, ?, ?, ?)",
        ("broken.test", time.time(), "well-known", "{not valid json"),
    )
    cache._conn.commit()
    assert cache.get("broken.test") is None
    cache.close()


# --- end-to-end: two separate clients, simulating a process restart -------


def test_second_client_skips_well_known_fetch_using_disk_cache(db_path):
    well_known_calls = {"n": 0}
    well_known_body = json.dumps({"default": {"requests_per_second": 0.75}, "contact": "mailto:ops@x.com"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/considerate.json":
            well_known_calls["n"] += 1
            return httpx.Response(200, text=well_known_body)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"ok")

    config = ConsiderateConfig(policy_cache_path=db_path)

    client1 = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))
    client1.get("https://restarttest.test/page")
    client1.close()
    assert well_known_calls["n"] == 1
    assert client1._domains["restarttest.test"].controller.rate == 0.75

    # A brand-new client, pointed at the same cache file — the well-known
    # fetch must NOT happen again.
    client2 = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))
    client2.get("https://restarttest.test/page")
    client2.close()

    assert well_known_calls["n"] == 1  # still 1 — served from disk, not the network
    assert client2._domains["restarttest.test"].controller.rate == 0.75


def test_disk_cache_does_not_skip_robots_txt_disallow_check(db_path):
    """The disk cache short-circuits the well-known fetch, not robots.txt —
    disallow rules must still apply on a "restarted" client.
    """
    well_known_body = json.dumps({"default": {"requests_per_second": 1.0}})
    robots_body = "User-agent: *\nDisallow: /private\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/considerate.json":
            return httpx.Response(200, text=well_known_body)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_body)
        return httpx.Response(200, content=b"ok")

    config = ConsiderateConfig(policy_cache_path=db_path)

    client1 = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))
    client1.get("https://robotscache.test/page")
    client1.close()

    from considerate import DisallowedError

    client2 = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedError):
        client2.get("https://robotscache.test/private/data")
    client2.close()
