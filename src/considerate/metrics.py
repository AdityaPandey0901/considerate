"""Optional Prometheus metrics export.

    from prometheus_client import start_http_server
    from considerate import ConsiderateClient
    from considerate.metrics import prometheus_event_handler

    client = ConsiderateClient(on_event=prometheus_event_handler())
    start_http_server(9100)  # metrics now show up at :9100/metrics

This is deliberately built on the existing `on_event` mechanism rather than
a parallel instrumentation path: every signal it exports is exactly what an
`Event` already carries (see events.py), so a metrics exporter and a
plain-text logger are just two different consumers of the same stream.

Requires the optional `prometheus-client` dependency:
`pip install considerate[observability]`.
"""

from __future__ import annotations

from typing import Callable

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge
    from prometheus_client import REGISTRY as _DEFAULT_REGISTRY
except ImportError as exc:  # pragma: no cover - exercised via the error message
    raise ImportError(
        "considerate.metrics requires prometheus-client: pip install considerate[observability]"
    ) from exc

from .events import Event

_RATE_INCREASE_EVENTS = frozenset({"rate_increased"})
_RATE_DECREASE_EVENTS = frozenset({"rate_decreased"})


def prometheus_event_handler(registry: "CollectorRegistry | None" = None) -> Callable[[Event], None]:
    """Build an `on_event` callback that records considerate `Event`s as
    Prometheus metrics on `registry` (the global default registry if
    omitted). Call this once per process — like any `prometheus_client`
    metric, registering the same metric name on the same registry twice
    raises `ValueError`.

    Exports:
        considerate_events_total{domain, kind} — every event, by kind.
        considerate_current_rate_req_per_second{domain} — the AIMD
            controller's rate, updated on every rate_increased/decreased.
        considerate_circuit_open_total{domain} — circuit breaker trips.
    """
    registry = registry or _DEFAULT_REGISTRY

    events_total = Counter(
        "considerate_events_total", "considerate events, by domain and kind", ["domain", "kind"], registry=registry
    )
    current_rate = Gauge(
        "considerate_current_rate_req_per_second",
        "Current AIMD controller rate for a domain",
        ["domain"],
        registry=registry,
    )
    circuit_open_total = Counter(
        "considerate_circuit_open_total", "Times the circuit breaker opened for a domain", ["domain"], registry=registry
    )

    def handler(event: Event) -> None:
        events_total.labels(domain=event.domain, kind=event.kind).inc()

        if event.kind in _RATE_INCREASE_EVENTS or event.kind in _RATE_DECREASE_EVENTS:
            rate = event.data.get("new_rate")
            if rate is not None:
                current_rate.labels(domain=event.domain).set(rate)

        if event.kind == "circuit_open":
            circuit_open_total.labels(domain=event.domain).inc()

    return handler
