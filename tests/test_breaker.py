import time

from considerate.breaker import BreakerConfig, BreakerState, CircuitBreaker


def test_starts_closed_and_allows_requests():
    breaker = CircuitBreaker()
    allowed, retry_after = breaker.check()
    assert allowed is True
    assert retry_after == 0.0


def test_opens_after_consecutive_failures():
    breaker = CircuitBreaker(BreakerConfig(consecutive_failures=3, cooldown_seconds=60))
    breaker.report_failure("timeout")
    breaker.report_failure("timeout")
    assert breaker.state is BreakerState.CLOSED
    breaker.report_failure("timeout")
    assert breaker.state is BreakerState.OPEN
    allowed, retry_after = breaker.check()
    assert allowed is False
    assert retry_after > 0


def test_opens_on_error_rate_even_without_consecutive_streak():
    config = BreakerConfig(consecutive_failures=100, error_rate_threshold=0.3, error_rate_window=10)
    breaker = CircuitBreaker(config)
    # Interleave successes/failures so the consecutive-failure counter keeps
    # resetting, but the rolling error rate still crosses the threshold.
    pattern = [True, False, True, False, True, False, True, False, True, False]
    for ok in pattern:
        if ok:
            breaker.report_success()
        else:
            breaker.report_failure("http_503")
    assert breaker.state is BreakerState.OPEN


def test_half_open_probe_success_closes_circuit():
    config = BreakerConfig(consecutive_failures=1, cooldown_seconds=0.05)
    breaker = CircuitBreaker(config)
    breaker.report_failure("timeout")
    assert breaker.state is BreakerState.OPEN
    time.sleep(0.06)
    assert breaker.state is BreakerState.HALF_OPEN
    allowed, _ = breaker.check()
    assert allowed is True
    breaker.report_success()
    assert breaker.state is BreakerState.CLOSED


def test_half_open_probe_failure_reopens_with_longer_cooldown():
    config = BreakerConfig(consecutive_failures=1, cooldown_seconds=0.05)
    breaker = CircuitBreaker(config)
    breaker.report_failure("timeout")
    time.sleep(0.06)
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.report_failure("timeout")
    assert breaker.state is BreakerState.OPEN
    assert breaker.cooldown > config.cooldown_seconds  # backed off further


def test_structured_payload_shape_via_client_exception():
    from considerate.exceptions import CircuitOpenError

    err = CircuitOpenError("example.com", "http_503", 42.0)
    assert err.payload == {
        "status": "circuit_open",
        "domain": "example.com",
        "reason": "http_503",
        "retry_after": 42.0,
    }
