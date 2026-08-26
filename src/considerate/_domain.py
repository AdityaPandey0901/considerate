"""Per-domain state: the glue between policy discovery, the AIMD controller,
and the circuit breaker. One `DomainState` lives for the lifetime of a
client, keyed by hostname, and is shared across sync/async paths — only the
"wait for a token" step differs between them (see client.py).
"""

from __future__ import annotations

import time
from datetime import datetime

from .breaker import CircuitBreaker
from .config import ConsiderateConfig
from .controller import AimdController
from .policy import SitePolicy

# Response headers that suggest a well-provisioned edge/CDN in front of the
# origin (Cloudflare, Fastly, Akamai, generic reverse proxies, ...). Presence
# is treated as a "robust" signal; absence plus a slow TTFB is treated as a
# "fragile" one. This is a heuristic, not a guarantee — a policy file or
# robots.txt crawl-delay always takes precedence when present.
_ROBUST_HEADER_HINTS = ("cf-ray", "x-served-by", "x-cache", "x-fastly-request-id", "server")
_ROBUST_SERVER_TOKENS = ("cloudflare", "fastly", "akamai", "vercel", "cloudfront")


class DomainState:
    def __init__(
        self,
        host: str,
        config: ConsiderateConfig,
        agent_name: str | None,
        verified_identity: str | None = None,
    ) -> None:
        self.host = host
        self.config = config
        self.agent_name = agent_name
        self.verified_identity = verified_identity

        self.policy: SitePolicy | None = None
        self.policy_fetched_at: float | None = None
        self.robots_parser = None  # urllib.robotparser.RobotFileParser, once fetched
        self.calibrated = False  # has the first real request refined our tier guess?

        tier = config.tier_for_domain(host)
        initial_rate = config.tier_rates.get(tier, config.tier_rates["standard"])
        ceiling = config.tier_ceilings.get(tier, config.tier_ceilings["standard"])

        self.controller = AimdController(initial_rate=initial_rate, burst=3, config=config.controller)
        self.base_ceiling = ceiling  # before any crawl_windows multiplier
        self.controller.set_ceiling(ceiling)
        self.hard_ceiling = False  # True once an explicit site policy caps us

        self.breaker = CircuitBreaker(config=config.breaker)

        # Concurrency limit as a plain int, not a live primitive: the sync
        # and async clients each own their own semaphore type (threading vs.
        # asyncio) keyed off this value, rather than DomainState owning one
        # concrete implementation neither client type actually wants (see
        # client.py's `_get_semaphore`). This also means changing the limit
        # (via apply_policy) can't silently discard in-flight holders of an
        # old semaphore object.
        self.max_concurrent = config.max_concurrent_per_domain

        # A hard floor on the next request time to this domain, set from a
        # `Retry-After` response header (SPEC.md §1.1: "MUST honor
        # Retry-After as a floor, not a suggestion") — independent of, and
        # enforced in addition to, the AIMD token bucket's own pacing.
        self.retry_not_before: float | None = None

    # -- policy application ---------------------------------------------------

    def policy_is_fresh(self) -> bool:
        return (
            self.policy_fetched_at is not None
            and (time.monotonic() - self.policy_fetched_at) < self.config.policy_cache_ttl
        )

    def apply_policy(self, policy: SitePolicy) -> None:
        self.policy = policy
        self.policy_fetched_at = time.monotonic()

        rule = policy.rule_for(self.agent_name, self.verified_identity)
        rate = rule.requests_per_second
        if rate is None and rule.tier:
            rate = self.config.tier_rates.get(rule.tier)
        if rate is None:
            rate = self.config.tier_rates[self.config.default_tier]

        self.controller.rate = min(self.controller.rate, rate) if self.calibrated else rate
        self.base_ceiling = rate  # explicit policy is a hard ceiling
        self.hard_ceiling = True
        self.refresh_effective_ceiling()

        if rule.max_concurrent:
            self.max_concurrent = rule.max_concurrent
        if rule.burst:
            self.controller.capacity = max(1, rule.burst)

    def refresh_effective_ceiling(self, now: datetime | None = None) -> None:
        """Re-derive the controller's ceiling from `base_ceiling` and any
        active `crawl_windows` multiplier. Cheap and side-effect-free beyond
        the controller update, so callers can call this on every request —
        a window opening or closing mid-run takes effect immediately rather
        than only at the next policy re-fetch (which may be 24h away).
        """
        multiplier = self.policy.active_multiplier(now) if self.policy else 1.0
        self.controller.set_ceiling(self.base_ceiling * multiplier)

    def is_path_disallowed(self, path: str) -> bool:
        return self.policy.is_path_disallowed(path) if self.policy else False

    def note_retry_after(self, seconds: float) -> None:
        """Record a `Retry-After`-derived floor. Only ever moves the floor
        later, never earlier — a fresh, longer Retry-After should extend the
        pause; a stale, already-elapsed one from an earlier response
        shouldn't shorten a floor a *later* response just set.
        """
        candidate = time.monotonic() + seconds
        self.retry_not_before = max(self.retry_not_before or 0.0, candidate)

    def retry_wait_remaining(self) -> float:
        if self.retry_not_before is None:
            return 0.0
        return max(0.0, self.retry_not_before - time.monotonic())

    def apply_inference(self, ttfb: float, headers) -> None:
        """Refine the initial tier guess from the very first real request's
        timing/headers — no separate probe request, per the spec's "no burst
        probing" principle. No-op if an explicit policy already applies.
        """
        self.calibrated = True
        if self.hard_ceiling:
            return

        looks_robust = any(
            any(tok in (headers.get(h, "") or "").lower() for tok in _ROBUST_SERVER_TOKENS)
            for h in _ROBUST_HEADER_HINTS
        ) or any(h in headers for h in ("cf-ray", "x-fastly-request-id"))

        if looks_robust:
            tier = "robust"
        elif ttfb > self.config.ttfb_fragile_threshold:
            tier = "fragile"
        else:
            tier = "standard"

        ceiling = self.config.tier_ceilings.get(tier, self.config.tier_ceilings["standard"])
        self.base_ceiling = ceiling
        self.refresh_effective_ceiling()
        # Nudge the running rate toward the inferred tier's starting point
        # rather than snapping to it, so a lucky/unlucky first request can't
        # cause a sharp jump.
        target = self.config.tier_rates.get(tier, self.controller.rate)
        self.controller.rate = (self.controller.rate + target) / 2
