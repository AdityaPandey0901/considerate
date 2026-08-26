"""Per-domain state: the glue between policy discovery, the AIMD controller,
and the circuit breaker. One `DomainState` lives for the lifetime of a
client, keyed by hostname, and is shared across sync/async paths — only the
"wait for a token" step differs between them (see client.py).
"""

from __future__ import annotations

import threading
import time

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
    def __init__(self, host: str, config: ConsiderateConfig, agent_name: str | None) -> None:
        self.host = host
        self.config = config
        self.agent_name = agent_name

        self.policy: SitePolicy | None = None
        self.policy_fetched_at: float | None = None
        self.robots_parser = None  # urllib.robotparser.RobotFileParser, once fetched
        self.calibrated = False  # has the first real request refined our tier guess?

        tier = config.tier_for_domain(host)
        initial_rate = config.tier_rates.get(tier, config.tier_rates["standard"])
        ceiling = config.tier_ceilings.get(tier, config.tier_ceilings["standard"])

        self.controller = AimdController(initial_rate=initial_rate, burst=3, config=config.controller)
        self.controller.set_ceiling(ceiling)
        self.hard_ceiling = False  # True once an explicit site policy caps us

        self.breaker = CircuitBreaker(config=config.breaker)
        self.semaphore = threading.Semaphore(config.max_concurrent_per_domain)

    # -- policy application ---------------------------------------------------

    def policy_is_fresh(self) -> bool:
        return (
            self.policy_fetched_at is not None
            and (time.monotonic() - self.policy_fetched_at) < self.config.policy_cache_ttl
        )

    def apply_policy(self, policy: SitePolicy) -> None:
        self.policy = policy
        self.policy_fetched_at = time.monotonic()

        rule = policy.rule_for(self.agent_name)
        rate = rule.requests_per_second
        if rate is None and rule.tier:
            rate = self.config.tier_rates.get(rule.tier)
        if rate is None:
            rate = self.config.tier_rates[self.config.default_tier]

        self.controller.rate = min(self.controller.rate, rate) if self.calibrated else rate
        self.controller.set_ceiling(rate)  # explicit policy is a hard ceiling
        self.hard_ceiling = True

        if rule.max_concurrent:
            self.semaphore = threading.Semaphore(rule.max_concurrent)
        if rule.burst:
            self.controller.capacity = max(1, rule.burst)

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
        self.controller.set_ceiling(ceiling)
        # Nudge the running rate toward the inferred tier's starting point
        # rather than snapping to it, so a lucky/unlucky first request can't
        # cause a sharp jump.
        target = self.config.tier_rates.get(tier, self.controller.rate)
        self.controller.rate = (self.controller.rate + target) / 2
