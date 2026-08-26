"""F4: opt-in smoke tests against the real internet. Excluded from a plain
`pytest` run (see the `network` marker + addopts in pyproject.toml) — run
explicitly with `pytest -m network`, e.g. on a schedule, not on every push.
They exist because MockTransport-based tests can silently drift from how
httpx actually behaves against a real server; nothing here replaces the
mocked suite, it's a periodic reality check on top of it.
"""

import pytest

from considerate import AgentIdentity, ConsiderateClient, DisallowedError

pytestmark = pytest.mark.network


def test_example_com_round_trip():
    # example.com is IANA-reserved specifically for documentation/testing
    # use — about as stable a target as the public internet offers.
    client = ConsiderateClient(identity=AgentIdentity(name="ConsiderateNetworkSmokeTest"))
    try:
        response = client.get("https://example.com/")
        assert response.status_code == 200
        state = client._domains["example.com"]
        assert state.calibrated is True
        assert state.controller.rate > 0
    finally:
        client.close()


def test_real_robots_txt_disallow_is_respected():
    # google.com/search has disallowed crawling via robots.txt for a very
    # long time — about as durable a real-world Disallow rule as exists.
    client = ConsiderateClient(identity=AgentIdentity(name="ConsiderateNetworkSmokeTest"))
    try:
        with pytest.raises(DisallowedError):
            client.get("https://www.google.com/search?q=test")
    finally:
        client.close()


def test_cli_inspect_against_a_real_url_exits_cleanly():
    from considerate.cli import build_parser, cmd_inspect

    parser = build_parser()
    args = parser.parse_args(["inspect", "https://example.com/", "--json"])
    assert cmd_inspect(args) == 0
