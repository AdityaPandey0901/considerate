"""The Adaptive Rate Controller: an AIMD token bucket, same family of
algorithm as TCP congestion control.

Start cautious. Speed up slowly and only when things are clearly fine.
Slow down immediately and by a lot at the first sign of trouble. That
asymmetry (additive increase, multiplicative decrease) is what makes AIMD
converge to "as fast as safely possible" instead of oscillating between
"too slow" and "took the site down."
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ControllerConfig:
    min_rate: float = 0.02  # requests/sec floor — never fully stop backing off
    max_rate: float = 10.0  # requests/sec ceiling, overridable per domain
    additive_step: float = 0.1  # requests/sec added per success streak
    decrease_factor: float = 0.5  # multiplied into rate on any bad signal
    success_streak_for_increase: int = 10
    latency_degradation_multiplier: float = 2.5  # vs rolling baseline TTFB


class AimdController:
    """A token bucket whose refill rate adapts to live success/failure signal.

    One instance per domain. Thread-safe (a single lock guards all state);
    the async client uses the same class and just awaits the computed wait
    time instead of blocking on it.
    """

    def __init__(self, initial_rate: float, burst: int = 3, config: ControllerConfig | None = None) -> None:
        self.config = config or ControllerConfig()
        self.rate = max(initial_rate, self.config.min_rate)
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._success_streak = 0
        self._baseline_latency: float | None = None
        self._lock = threading.Lock()

    # -- token bucket mechanics -------------------------------------------------

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last_refill = now

    def wait_time(self) -> float:
        """Seconds the caller must wait before a token will be available.

        Returns 0.0 if a token is available right now. Does not consume it —
        call `consume()` (after any sleep) to actually take the token.
        """
        with self._lock:
            self._refill_locked()
            if self.tokens >= 1.0:
                return 0.0
            missing = 1.0 - self.tokens
            return missing / self.rate if self.rate > 0 else float("inf")

    def consume(self) -> None:
        with self._lock:
            self._refill_locked()
            self.tokens = max(0.0, self.tokens - 1.0)

    # -- AIMD feedback ------------------------------------------------------

    def report_success(self, latency: float | None = None) -> None:
        """Record a clean response. May trigger additive rate increase."""
        with self._lock:
            if latency is not None:
                if self._baseline_latency is None:
                    self._baseline_latency = latency
                else:
                    # Exponential moving average, slow to move so a single
                    # fast response doesn't erase a real degradation trend.
                    self._baseline_latency = 0.9 * self._baseline_latency + 0.1 * latency

                degraded = (
                    self._baseline_latency is not None
                    and latency > self._baseline_latency * self.config.latency_degradation_multiplier
                    and latency > 0.3  # ignore noise on already-fast sites
                )
                if degraded:
                    self._decrease_locked()
                    return

            self._success_streak += 1
            if self._success_streak >= self.config.success_streak_for_increase:
                self._success_streak = 0
                self.rate = min(self.config.max_rate, self.rate + self.config.additive_step)

    def report_failure(self) -> None:
        """Record a degradation signal: timeout, 429, 503, connection reset."""
        with self._lock:
            self._decrease_locked()

    def _decrease_locked(self) -> None:
        self._success_streak = 0
        self.rate = max(self.config.min_rate, self.rate * self.config.decrease_factor)

    def set_ceiling(self, max_rate: float) -> None:
        with self._lock:
            self.config.max_rate = max_rate
            self.rate = min(self.rate, max_rate)
