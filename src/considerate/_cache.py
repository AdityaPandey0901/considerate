"""An optional on-disk policy cache (sqlite) so a short-lived process
(a script, a Lambda, a CLI invocation) doesn't rediscover every domain's
policy from scratch on every cold start. The in-memory `DomainState` cache
(policy_cache_ttl, checked via `time.monotonic()`) already covers a single
process's lifetime; this covers the gap between processes.

Deliberately its own tiny module rather than folded into policy.py: it's
the one place in the package that touches a filesystem, and keeping I/O
boundaries obvious matters more here than avoiding a short file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from .policy import RateRule, SitePolicy, parse_well_known


def _rule_to_dict(rule: RateRule) -> dict:
    return {
        k: v
        for k, v in (
            ("requests_per_second", rule.requests_per_second),
            ("max_concurrent", rule.max_concurrent),
            ("burst", rule.burst),
            ("tier", rule.tier),
            ("note", rule.note),
        )
        if v is not None
    }


def policy_to_wire_dict(policy: SitePolicy) -> dict:
    """The same shape as a `/.well-known/considerate.json` document — so
    caching round-trips through the exact parser real policy files use,
    rather than a second, cache-specific (de)serializer.
    """
    return {
        "version": policy.version,
        "contact": policy.contact,
        "default": _rule_to_dict(policy.default),
        "agents": {name: _rule_to_dict(r) for name, r in policy.agents.items()},
        "verified_agents": {name: _rule_to_dict(r) for name, r in policy.verified_agents.items()},
        "disallow_paths": policy.disallow_paths,
        "crawl_windows": [
            {k: v for k, v in {"days": list(w.days), "hours": w.hours, "multiplier": w.multiplier, "note": w.note}.items() if v is not None}
            for w in policy.crawl_windows
        ],
    }


class PersistentPolicyCache:
    """A tiny sqlite-backed `host -> (SitePolicy, fetched_at wall-clock time)`
    store. `fetched_at` is `time.time()`, not `time.monotonic()` — it has to
    survive process restarts, so it must be a wall-clock timestamp; TTL
    freshness against it is the caller's job (client.py), since only the
    caller knows the configured `policy_cache_ttl`.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS policies ("
                "host TEXT PRIMARY KEY, fetched_at REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL)"
            )
            self._conn.commit()

    def get(self, host: str) -> tuple[SitePolicy, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, source, data FROM policies WHERE host = ?", (host,)
            ).fetchone()
        if row is None:
            return None
        fetched_at, source, data = row
        try:
            policy = parse_well_known(data)
        except Exception:
            return None  # a corrupted/older-schema row is treated as a cache miss, not an error
        policy.source = source
        return policy, fetched_at

    def set(self, host: str, policy: SitePolicy, fetched_at: float) -> None:
        data = json.dumps(policy_to_wire_dict(policy))
        with self._lock:
            self._conn.execute(
                "INSERT INTO policies (host, fetched_at, source, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "source=excluded.source, data=excluded.data",
                (host, fetched_at, policy.source, data),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def load_fresh(cache: PersistentPolicyCache, host: str, ttl: float) -> SitePolicy | None:
    """The cached policy for `host`, or None if there isn't one or it's
    past `ttl`. A separate function (not a `PersistentPolicyCache` method)
    because freshness is relative to the caller's configured TTL, which
    the cache itself has no opinion about.
    """
    cached = cache.get(host)
    if cached is None:
        return None
    policy, fetched_at = cached
    if time.time() - fetched_at >= ttl:
        return None
    return policy
