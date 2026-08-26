"""Site-side half of the handshake: what a site is telling agents about its capacity.

Three sources are consulted, in order of trust, for a given domain:

1. `/.well-known/considerate.json` — an explicit, structured policy a site
   operator publishes. If present, it wins outright.
2. `robots.txt` — specifically `Crawl-delay`, a directive most sites that
   care already publish for search bots. considerate treats it as a lower
   bound on delay-between-requests.
3. Nothing — considerate falls back to runtime inference (see estimator.py).

This module only concerns itself with (1) and (2): fetching/parsing
declared policy. It does no network I/O itself — callers pass in already
fetched bytes so this stays trivially unit-testable and reusable from both
the sync and async clients.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib import robotparser

from .exceptions import PolicyError

SPEC_VERSION = "0.1"

_VALID_TIERS = {"fragile", "standard", "robust"}


@dataclass
class RateRule:
    """A single rate rule — either the site-wide default or a per-agent override."""

    requests_per_second: float | None = None
    max_concurrent: int | None = None
    burst: int | None = None
    tier: str | None = None
    note: str | None = None


@dataclass
class SitePolicy:
    """Parsed `/.well-known/considerate.json`.

    See SPEC.md section 2 for the full schema. Unknown fields are ignored
    (forward compatibility), and every field is optional so a site can
    publish as little or as much as it wants.
    """

    version: str = SPEC_VERSION
    contact: str | None = None
    default: RateRule = field(default_factory=RateRule)
    agents: dict[str, RateRule] = field(default_factory=dict)
    disallow_paths: list[str] = field(default_factory=list)
    source: str = "well-known"  # "well-known" | "robots-crawl-delay" | "inferred"

    def rule_for(self, agent_name: str | None) -> RateRule:
        """Return the most specific rule that applies to `agent_name`.

        Exact name match wins, then a `"*"` wildcard entry in `agents`,
        then the site-wide `default`.
        """
        if agent_name:
            if agent_name in self.agents:
                return self.agents[agent_name]
            if "*" in self.agents:
                return self.agents["*"]
        return self.default


def parse_well_known(raw: bytes | str) -> SitePolicy:
    """Parse the contents of a `/.well-known/considerate.json` document.

    Raises PolicyError on malformed JSON or an obviously-wrong shape.
    Anything more lenient (unknown keys, missing optional fields) is
    accepted silently — a policy file is a signal, not a contract, and a
    strict parser here would just encourage sites *not* to publish one.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise PolicyError(f"considerate.json is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PolicyError("considerate.json must be a JSON object")

    def _rule(obj: dict) -> RateRule:
        tier = obj.get("tier")
        if tier is not None and tier not in _VALID_TIERS:
            tier = None
        return RateRule(
            requests_per_second=_as_float(obj.get("requests_per_second")),
            max_concurrent=_as_int(obj.get("max_concurrent")),
            burst=_as_int(obj.get("burst")),
            tier=tier,
            note=obj.get("note"),
        )

    default_obj = data.get("default")
    default = _rule(default_obj) if isinstance(default_obj, dict) else RateRule()

    agents_obj = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    agents = {
        name: _rule(obj)
        for name, obj in agents_obj.items()
        if isinstance(obj, dict)
    }

    disallow = data.get("disallow_paths")
    disallow_paths = [p for p in disallow if isinstance(p, str)] if isinstance(disallow, list) else []

    return SitePolicy(
        version=str(data.get("version", SPEC_VERSION)),
        contact=data.get("contact") if isinstance(data.get("contact"), str) else None,
        default=default,
        agents=agents,
        disallow_paths=disallow_paths,
        source="well-known",
    )


def parse_robots_crawl_delay(robots_txt: str, user_agent: str = "*", url_for_check: str | None = None) -> tuple[SitePolicy | None, robotparser.RobotFileParser]:
    """Parse robots.txt for a `Crawl-delay` directive and return a SitePolicy.

    Returns (None, parser) if no crawl-delay applies (parser is still useful
    for the caller to run `.can_fetch()` against). We use the stdlib
    `robotparser` for correctness rather than hand-rolling group matching.
    """
    parser = robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())

    delay = parser.crawl_delay(user_agent)
    if delay is None:
        delay = parser.crawl_delay("*")
    if not delay or delay <= 0:
        return None, parser

    policy = SitePolicy(
        default=RateRule(requests_per_second=1.0 / float(delay)),
        source="robots-crawl-delay",
    )
    return policy, parser


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
