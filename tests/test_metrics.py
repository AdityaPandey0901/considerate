"""Tests for the optional Prometheus exporter (C4)."""

import httpx
import pytest

prometheus_client = pytest.importorskip("prometheus_client")
from prometheus_client import CollectorRegistry  # noqa: E402

from considerate import ConsiderateClient, ConsiderateConfig  # noqa: E402
from considerate.breaker import BreakerConfig  # noqa: E402
from considerate.controller import ControllerConfig  # noqa: E402
from considerate.metrics import prometheus_event_handler  # noqa: E402

# A single successful request doesn't move the rate under real defaults
# (the controller needs a streak of 10 clean responses before climbing) —
# force it to 1 so these tests can assert on one request deterministically.
_INSTANT_INCREASE = ConsiderateConfig(controller=ControllerConfig(success_streak_for_increase=1))


def _handler(status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(status)

    return handler


def test_events_total_counter_increments():
    registry = CollectorRegistry()
    client = ConsiderateClient(
        config=_INSTANT_INCREASE, on_event=prometheus_event_handler(registry), transport=httpx.MockTransport(_handler())
    )
    client.get("https://metrics1.test/page")

    value = registry.get_sample_value(
        "considerate_events_total", {"domain": "metrics1.test", "kind": "rate_increased"}
    )
    assert value == 1.0


def test_current_rate_gauge_reflects_controller_rate():
    registry = CollectorRegistry()
    client = ConsiderateClient(
        config=_INSTANT_INCREASE, on_event=prometheus_event_handler(registry), transport=httpx.MockTransport(_handler())
    )
    client.get("https://metrics2.test/page")

    rate = registry.get_sample_value("considerate_current_rate_req_per_second", {"domain": "metrics2.test"})
    expected = client._domains["metrics2.test"].controller.rate
    assert rate == pytest.approx(expected)


def test_circuit_open_counter_increments_on_trip():
    registry = CollectorRegistry()
    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=1))
    client = ConsiderateClient(
        config=config, on_event=prometheus_event_handler(registry), transport=httpx.MockTransport(_handler(503))
    )
    client.get("https://metrics3.test/page")  # trips the breaker (threshold=1)

    from considerate import CircuitOpenError

    with pytest.raises(CircuitOpenError):
        client.get("https://metrics3.test/page")

    total = registry.get_sample_value("considerate_circuit_open_total", {"domain": "metrics3.test"})
    assert total == 1.0


def test_registering_twice_on_the_same_registry_raises():
    registry = CollectorRegistry()
    prometheus_event_handler(registry)
    with pytest.raises(ValueError):
        prometheus_event_handler(registry)
