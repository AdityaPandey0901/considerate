"""Structured events emitted during a session, for observability.

Pass a callback to `on_event=` and get called for every rate change, breaker
trip, and policy discovery. This is what lets an agent report honestly to
its user ("I slowed down on example.com because it looked fragile") instead
of the slowdown happening silently inside the library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable

EventCallback = Callable[["Event"], None]


@dataclass
class Event:
    kind: str  # "policy_discovered" | "rate_changed" | "circuit_open" | "circuit_closed" | "disallowed"
    domain: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time)


def emit(callback: EventCallback | None, kind: str, domain: str, **data: Any) -> None:
    if callback is None:
        return
    try:
        callback(Event(kind=kind, domain=domain, data=data))
    except Exception:
        # Observability must never break the request path.
        pass
