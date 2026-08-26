"""Small internal helpers with no considerate-specific dependencies."""

from __future__ import annotations

import socket
import ssl
from email.utils import parsedate_to_datetime

import httpx

# Stable, public failure-reason vocabulary for CircuitOpenError.payload and
# breaker/event reasons — deliberately not just `type(exc).__name__`, which
# would tie the public contract to httpx's internal exception hierarchy and
# break silently if httpx ever renames/restructures it.
_TIMEOUT = "timeout"
_DNS_ERROR = "dns_error"
_TLS_ERROR = "tls_error"
_PROTOCOL_ERROR = "protocol_error"
_CONNECTION_ERROR = "connection_error"
_TRANSPORT_ERROR = "transport_error"


def classify_transport_failure(exc: BaseException) -> str:
    """Map an httpx request exception to one of a small, stable set of
    reason strings (see the constants above), inspecting the wrapped
    stdlib exception (`__cause__`) where httpx doesn't have a dedicated
    subclass for the distinction (DNS resolution, TLS) — both surface as a
    plain `httpx.ConnectError` today.
    """
    if isinstance(exc, httpx.TimeoutException):
        return _TIMEOUT

    if isinstance(exc, httpx.TransportError):
        cause = exc.__cause__
        text = f"{exc} {cause}".lower()

        if isinstance(cause, socket.gaierror) or "getaddrinfo" in text or "name or service not known" in text:
            return _DNS_ERROR
        if isinstance(cause, ssl.SSLError) or "ssl" in text or "certificate" in text:
            return _TLS_ERROR
        if isinstance(exc, (httpx.ProtocolError, httpx.LocalProtocolError, httpx.RemoteProtocolError)):
            return _PROTOCOL_ERROR
        return _CONNECTION_ERROR

    return _TRANSPORT_ERROR


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
