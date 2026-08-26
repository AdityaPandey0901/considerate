"""Small internal helpers with no considerate-specific dependencies."""

from __future__ import annotations

from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header (RFC 9110 §10.2.3) into seconds from now.

    Accepts either form the spec allows: an integer delta-seconds, or an
    HTTP-date. Returns None for anything unparseable rather than raising —
    a malformed header should degrade to "no explicit floor," not crash the
    request path.
    """
    if not value:
        return None
    value = value.strip()

    try:
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        pass

    try:
        import time as _time

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            # RFC 9110 HTTP-dates are always GMT; treat naive as UTC.
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt.timestamp() - _time.time()
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None
