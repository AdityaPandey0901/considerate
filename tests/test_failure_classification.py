"""Tests for C6: transport failures get a stable, specific reason string
instead of a raw exception class name."""

import socket
import ssl

import httpx
import pytest

from considerate._util import classify_transport_failure


def test_timeout_classified_as_timeout():
    assert classify_transport_failure(httpx.ConnectTimeout("x")) == "timeout"
    assert classify_transport_failure(httpx.ReadTimeout("x")) == "timeout"
    assert classify_transport_failure(httpx.PoolTimeout("x")) == "timeout"


def test_dns_failure_classified_via_cause():
    exc = httpx.ConnectError("Name or service not known")
    exc.__cause__ = socket.gaierror("simulated DNS failure")
    assert classify_transport_failure(exc) == "dns_error"


def test_tls_failure_classified_via_cause():
    exc = httpx.ConnectError("SSL handshake failed")
    exc.__cause__ = ssl.SSLError("simulated TLS failure")
    assert classify_transport_failure(exc) == "tls_error"


def test_plain_connect_error_classified_as_connection_error():
    exc = httpx.ConnectError("Connection refused")
    assert classify_transport_failure(exc) == "connection_error"


def test_protocol_error_classified_as_protocol_error():
    assert classify_transport_failure(httpx.RemoteProtocolError("x")) == "protocol_error"
    assert classify_transport_failure(httpx.LocalProtocolError("x")) == "protocol_error"


def test_unrecognized_transport_error_falls_back():
    class WeirdTransportError(httpx.TransportError):
        pass

    assert classify_transport_failure(WeirdTransportError("x")) == "connection_error"


def test_non_transport_exception_gets_generic_bucket():
    assert classify_transport_failure(ValueError("not a transport error")) == "transport_error"


def test_client_records_specific_reason_on_dns_failure():
    from considerate import ConsiderateClient, ConsiderateConfig
    from considerate.breaker import BreakerConfig

    def handler(request: httpx.Request) -> httpx.Response:
        exc = httpx.ConnectError("Name or service not known")
        exc.__cause__ = socket.gaierror("simulated")
        raise exc

    # A single failure needs to actually open the breaker for `.reason` to
    # be populated (it's only recorded when the circuit trips) — one
    # consecutive failure is enough to see the classified reason land.
    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=1))
    client = ConsiderateClient(config=config, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError):
        client.get("https://dnsfailtest.test/page")

    assert client._domains["dnsfailtest.test"].breaker.reason == "dns_error"
