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
from datetime import datetime, timezone
from urllib import robotparser

from .exceptions import PolicyError

SPEC_VERSION = "0.2"

_VALID_TIERS = {"fragile", "standard", "robust"}
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class RateRule:
    """A single rate rule — either the site-wide default or a per-agent override."""

    requests_per_second: float | None = None
    max_concurrent: int | None = None
    burst: int | None = None
    tier: str | None = None
    note: str | None = None


@dataclass
class CrawlWindow:
    """A time-of-day/day-of-week rate multiplier (SPEC.md §2, v0.2).

    All times are UTC — a policy file has no way to express a timezone
    today, and UTC is at least unambiguous. `hours` is "HH:MM-HH:MM" and
    may wrap past midnight (e.g. "22:00-06:00"). Empty `days` means every
    day. When multiple windows are simultaneously active, the largest
    multiplier applies.
    """

    days: tuple[str, ...] = ()
    hours: str | None = None
    multiplier: float = 1.0
    note: str | None = None

    def is_active(self, now: datetime) -> bool:
        if self.days and _DAY_NAMES[now.weekday()] not in self.days:
            return False
        if not self.hours:
            return True
        try:
            start_s, end_s = self.hours.split("-", 1)
            start, end = _minutes_since_midnight(start_s), _minutes_since_midnight(end_s)
        except ValueError:
            return False
        current = now.hour * 60 + now.minute
        if start <= end:
            return start <= current < end
        return current >= start or current < end  # wraps past midnight


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
    # Web Bot Auth-verified identities (SPEC.md §6/v0.2, experimental): keyed
    # by whatever stable identifier the verification step produced (e.g. a
    # Signature Agent `client_id`), not the self-declared header name. When
    # a caller supplies a verified_identity, a match here outranks even an
    # exact `agents` name match, since it's backed by something stronger
    # than a courtesy header. considerate does not perform the verification
    # itself — see client.py's `verified_identity` parameter.
    verified_agents: dict[str, RateRule] = field(default_factory=dict)
    disallow_paths: list[str] = field(default_factory=list)
    crawl_windows: list[CrawlWindow] = field(default_factory=list)
    source: str = "well-known"  # "well-known" | "robots-crawl-delay" | "inferred"

    def rule_for(self, agent_name: str | None, verified_identity: str | None = None) -> RateRule:
        """Return the most specific rule that applies.

        Priority: a verified identity match, then an exact self-declared
        name match, then a `"*"` wildcard entry in `agents`, then the
        site-wide `default`.
        """
        if verified_identity and verified_identity in self.verified_agents:
            return self.verified_agents[verified_identity]
        if agent_name:
            if agent_name in self.agents:
                return self.agents[agent_name]
            if "*" in self.agents:
                return self.agents["*"]
        return self.default

    def active_multiplier(self, now: datetime | None = None) -> float:
        """The largest crawl_windows multiplier active right now (1.0 if
        none apply or none are configured).
        """
        if not self.crawl_windows:
            return 1.0
        now = now or datetime.now(timezone.utc)
        applicable = [w.multiplier for w in self.crawl_windows if w.is_active(now)]
        return max(applicable) if applicable else 1.0

    def is_path_disallowed(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.disallow_paths)


def _minutes_since_midnight(hhmm: str) -> int:
    hour_s, minute_s = hhmm.strip().split(":", 1)
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time of day: {hhmm!r}")
    return hour * 60 + minute


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

    def _agent_map(key: str) -> dict[str, RateRule]:
        raw_obj = data.get(key)
        obj: dict = raw_obj if isinstance(raw_obj, dict) else {}
        return {name: _rule(rule_obj) for name, rule_obj in obj.items() if isinstance(rule_obj, dict)}

    agents = _agent_map("agents")
    verified_agents = _agent_map("verified_agents")

    disallow = data.get("disallow_paths")
    disallow_paths = [p for p in disallow if isinstance(p, str)] if isinstance(disallow, list) else []

    crawl_windows: list[CrawlWindow] = []
    for w in data.get("crawl_windows") or []:
        if not isinstance(w, dict):
            continue
        days = (
            tuple(d for d in w.get("days", []) if isinstance(d, str) and d in _DAY_NAMES)
            if isinstance(w.get("days"), list)
            else ()
        )
        hours = w.get("hours") if isinstance(w.get("hours"), str) else None
        multiplier = _as_float(w.get("multiplier"))
        note = w.get("note") if isinstance(w.get("note"), str) else None
        crawl_windows.append(
            CrawlWindow(days=days, hours=hours, multiplier=multiplier if multiplier is not None else 1.0, note=note)
        )

    return SitePolicy(
        version=str(data.get("version", SPEC_VERSION)),
        contact=data.get("contact") if isinstance(data.get("contact"), str) else None,
        default=default,
        agents=agents,
        verified_agents=verified_agents,
        disallow_paths=disallow_paths,
        crawl_windows=crawl_windows,
        source="well-known",
    )


def parse_robots_crawl_delay(
    robots_txt: str, user_agent: str = "*", url_for_check: str | None = None
) -> tuple[SitePolicy | None, robotparser.RobotFileParser]:
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
    # typeshed types RobotFileParser.crawl_delay as returning `str | None`;
    # at runtime it's already numeric. Converting explicitly here satisfies
    # the type checker and is a harmless no-op either way.
    delay_seconds = float(delay) if delay is not None else None
    if not delay_seconds or delay_seconds <= 0:
        return None, parser

    policy = SitePolicy(
        default=RateRule(requests_per_second=1.0 / delay_seconds),
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
