"""Session-wide configuration. Every field has a sane default — the whole
point is that `ConsiderateClient()` with zero arguments already protects
sites, per the spec's "no config required for baseline protection" goal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .breaker import BreakerConfig
from .controller import ControllerConfig

TIER_DEFAULTS = {"fragile": 0.2, "standard": 1.0, "robust": 3.0}
TIER_CEILINGS = {"fragile": 0.5, "standard": 3.0, "robust": 10.0}


@dataclass
class ConsiderateConfig:
    default_tier: str = "standard"
    tier_rates: dict[str, float] = field(default_factory=lambda: dict(TIER_DEFAULTS))
    tier_ceilings: dict[str, float] = field(default_factory=lambda: dict(TIER_CEILINGS))
    overrides: dict[str, str] = field(default_factory=dict)  # domain -> tier

    respect_robots_txt: bool = True
    fetch_well_known: bool = True
    policy_cache_ttl: float = 24 * 3600.0
    max_concurrent_per_domain: int = 2
    max_tracked_domains: int = 2000  # LRU-evict the coldest domain past this
    policy_cache_path: str | None = None  # sqlite file for cross-process policy caching (C2); None disables it
    meta_fetch_max_bytes: int = 1_000_000  # cap on /.well-known + robots.txt fetches
    max_redirects: int = 5  # redirect hops followed *through* considerate's own metering

    ttfb_fragile_threshold: float = 0.8  # seconds
    infra_header_hint = ("cf-ray", "x-served-by", "x-cache", "via", "x-fastly-request-id")

    controller: ControllerConfig = field(default_factory=ControllerConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)

    def tier_for_domain(self, domain: str) -> str:
        return self.overrides.get(domain, self.default_tier)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConsiderateConfig":
        """Load config from a `considerate.yaml` file (see the shipped
        `considerate.yaml.example` for the schema). Requires the optional
        `pyyaml` dependency (`pip install considerate[yaml]`).
        """
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - exercised via error message
            raise ImportError(
                "considerate.yaml loading requires pyyaml: pip install considerate[yaml]"
            ) from exc

        raw = yaml.safe_load(Path(path).read_text()) or {}
        cfg = cls()

        if "default_tier" in raw:
            cfg.default_tier = raw["default_tier"]
        if "overrides" in raw and isinstance(raw["overrides"], dict):
            cfg.overrides = dict(raw["overrides"])
        if "respect_robots_txt" in raw:
            cfg.respect_robots_txt = bool(raw["respect_robots_txt"])
        if "fetch_well_known" in raw:
            cfg.fetch_well_known = bool(raw["fetch_well_known"])
        if "max_concurrent_per_domain" in raw:
            cfg.max_concurrent_per_domain = int(raw["max_concurrent_per_domain"])

        cb = raw.get("circuit_breaker") or {}
        if "error_threshold" in cb:
            cfg.breaker.error_rate_threshold = float(cb["error_threshold"])
        if "consecutive_failures" in cb:
            cfg.breaker.consecutive_failures = int(cb["consecutive_failures"])
        if "cooldown_seconds" in cb:
            cfg.breaker.cooldown_seconds = float(cb["cooldown_seconds"])

        return cfg
