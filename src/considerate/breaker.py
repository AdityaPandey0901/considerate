"""The Circuit Breaker: the "stop entirely" layer above the rate controller.

The AIMD controller answers "how fast is safe *right now*?" — it always
allows some traffic through, just slower. The breaker answers a different
question: "has this site clearly stopped coping, such that any traffic at
all is making it worse?" When that trips, considerate stops sending
requests to that domain outright until a single cooldown probe succeeds.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerConfig:
    consecutive_failures: int = 3
    error_rate_threshold: float = 0.2
    error_rate_window: int = 10  # min sample size before error-rate trips
    cooldown_seconds: float = 60.0
    max_cooldown_seconds: float = 900.0  # cap on repeated-open backoff


class CircuitBreaker:
    """One instance per domain. Thread-safe."""

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self.config = config or BreakerConfig()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._recent: deque[bool] = deque(maxlen=self.config.error_rate_window)
        self._opened_at: float | None = None
        self._cooldown = self.config.cooldown_seconds
        self._reason = ""
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return self._state

    def _maybe_transition_to_half_open_locked(self) -> None:
        if self._state is BreakerState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._cooldown:
                self._state = BreakerState.HALF_OPEN

    def check(self) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds).

        `allowed=False` means: send nothing, the circuit is open. In
        HALF_OPEN state, exactly one caller is let through as a probe —
        considerate does this by allowing the check and relying on the
        caller to report the probe's outcome before another check happens
        (the client serializes requests per domain, so this is safe).
        """
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            if self._state is BreakerState.OPEN:
                remaining = self._cooldown - (time.monotonic() - (self._opened_at or 0.0))
                return False, max(0.0, remaining)
            return True, 0.0

    def report_success(self) -> None:
        with self._lock:
            self._recent.append(True)
            self._consecutive_failures = 0
            if self._state is BreakerState.HALF_OPEN:
                # Probe succeeded: close up and reset backoff.
                self._state = BreakerState.CLOSED
                self._cooldown = self.config.cooldown_seconds
                self._opened_at = None

    def report_failure(self, reason: str) -> None:
        with self._lock:
            self._recent.append(False)
            self._consecutive_failures += 1

            if self._state is BreakerState.HALF_OPEN:
                # Probe failed: reopen, and back off further next time.
                self._open_locked(reason)
                self._cooldown = min(self.config.max_cooldown_seconds, self._cooldown * 2)
                return

            if self._state is BreakerState.OPEN:
                return

            error_rate = self._recent.count(False) / len(self._recent) if self._recent else 0.0
            tripped_by_streak = self._consecutive_failures >= self.config.consecutive_failures
            tripped_by_rate = (
                len(self._recent) >= self.config.error_rate_window
                and error_rate >= self.config.error_rate_threshold
            )
            if tripped_by_streak or tripped_by_rate:
                self._open_locked(reason)

    def _open_locked(self, reason: str) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = time.monotonic()
        self._reason = reason

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cooldown(self) -> float:
        return self._cooldown
