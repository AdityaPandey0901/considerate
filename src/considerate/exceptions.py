"""Exceptions raised by considerate.

All of these carry a `.payload` dict matching the structured shape described
in SPEC.md, so an agent/orchestrator can serialize it straight into a report
to the user or an LLM tool-result, e.g.:

    {"status": "circuit_open", "domain": "example.com", "reason": "...", "retry_after": 60}
"""

from __future__ import annotations

from typing import Any


class ConsiderateError(Exception):
    """Base class for all considerate errors."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = payload or {}


class CircuitOpenError(ConsiderateError):
    """Raised when the circuit breaker for a domain is open.

    This means: stop. Don't retry harder. Surface `.payload` to whoever is
    driving the agent so they can make an honest call about what to do next.
    """

    def __init__(self, domain: str, reason: str, retry_after: float) -> None:
        payload = {
            "status": "circuit_open",
            "domain": domain,
            "reason": reason,
            "retry_after": retry_after,
        }
        super().__init__(
            f"considerate: circuit open for {domain!r} ({reason}); retry after {retry_after:.0f}s",
            payload,
        )
        self.domain = domain
        self.reason = reason
        self.retry_after = retry_after


class DisallowedError(ConsiderateError):
    """Raised when robots.txt explicitly disallows the requested path.

    considerate is not a robots.txt engine, but refusing to send a request
    the site owner has explicitly disallowed is table stakes for a library
    with "safety" in its purpose — this is checked before any throttling
    logic runs, not instead of it.
    """

    def __init__(self, url: str) -> None:
        payload = {"status": "disallowed", "url": url}
        super().__init__(
            f"considerate: robots.txt disallows fetching {url!r} (set respect_robots_txt=False to override)",
            payload,
        )
        self.url = url


class PolicyError(ConsiderateError):
    """Raised for a malformed site policy document (considerate.json)."""
