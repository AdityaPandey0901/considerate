import json

import pytest

from considerate.exceptions import PolicyError
from considerate.policy import parse_robots_crawl_delay, parse_well_known


def test_parse_well_known_full_document():
    doc = json.dumps(
        {
            "version": "0.1",
            "contact": "mailto:ops@example.com",
            "default": {"requests_per_second": 0.5, "max_concurrent": 1},
            "agents": {"MyBot": {"requests_per_second": 2.0, "note": "trusted"}},
        }
    )
    policy = parse_well_known(doc)
    assert policy.contact == "mailto:ops@example.com"
    assert policy.default.requests_per_second == 0.5
    assert policy.rule_for("MyBot").requests_per_second == 2.0
    assert policy.rule_for("SomeoneElse").requests_per_second == 0.5


def test_wildcard_agent_rule_applies_to_unknown_named_agents():
    doc = json.dumps({"default": {"requests_per_second": 1.0}, "agents": {"*": {"requests_per_second": 0.1}}})
    policy = parse_well_known(doc)
    assert policy.rule_for("UnknownBot").requests_per_second == 0.1
    assert policy.rule_for(None).requests_per_second == 1.0  # no header at all -> default, not "*"


def test_invalid_json_raises_policy_error():
    with pytest.raises(PolicyError):
        parse_well_known("{not json")


def test_non_object_json_raises_policy_error():
    with pytest.raises(PolicyError):
        parse_well_known("[1, 2, 3]")


def test_unknown_fields_are_ignored():
    doc = json.dumps({"default": {"requests_per_second": 1.0}, "future_field": {"anything": True}})
    policy = parse_well_known(doc)
    assert policy.default.requests_per_second == 1.0


def test_invalid_tier_is_dropped_not_fatal():
    doc = json.dumps({"default": {"tier": "extremely-fragile"}})
    policy = parse_well_known(doc)
    assert policy.default.tier is None


ROBOTS_WITH_DELAY = """
User-agent: *
Crawl-delay: 2
Disallow: /admin
"""


def test_robots_crawl_delay_parsed_as_rate():
    policy, parser = parse_robots_crawl_delay(ROBOTS_WITH_DELAY, user_agent="MyBot")
    assert policy is not None
    assert policy.default.requests_per_second == pytest.approx(0.5)
    assert parser.can_fetch("MyBot", "https://example.com/admin/x") is False
    assert parser.can_fetch("MyBot", "https://example.com/page") is True


def test_robots_without_crawl_delay_returns_none_policy():
    policy, parser = parse_robots_crawl_delay("User-agent: *\nDisallow:\n", user_agent="MyBot")
    assert policy is None
    assert parser.can_fetch("MyBot", "https://example.com/anything") is True
