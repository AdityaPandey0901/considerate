"""F1: property-based tests on the AIMD controller. The scenario tests in
test_controller.py check specific sequences; these check invariants that
must hold after *any* sequence of success/failure/consume calls, which is
where the interesting edge cases in a stateful rate limiter tend to hide.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from considerate.controller import AimdController, ControllerConfig

_ops = st.lists(st.sampled_from(["success", "success_slow", "failure", "consume"]), max_size=200)


@st.composite
def _controller_and_ops(draw):
    min_rate = draw(st.floats(min_value=0.01, max_value=1.0))
    max_rate = draw(st.floats(min_value=min_rate + 0.01, max_value=min_rate + 20.0))
    config = ControllerConfig(
        min_rate=min_rate,
        max_rate=max_rate,
        additive_step=draw(st.floats(min_value=0.01, max_value=2.0)),
        decrease_factor=draw(st.floats(min_value=0.1, max_value=0.9)),
        success_streak_for_increase=draw(st.integers(min_value=1, max_value=15)),
    )
    initial_rate = draw(st.floats(min_value=min_rate, max_value=max_rate))
    ops = draw(_ops)
    return config, initial_rate, ops


@settings(max_examples=200)
@given(_controller_and_ops())
def test_rate_always_within_configured_bounds(data):
    config, initial_rate, ops = data
    controller = AimdController(initial_rate=initial_rate, burst=5, config=config)

    for op in ops:
        if op == "success":
            controller.report_success(latency=0.05)
        elif op == "success_slow":
            controller.report_success(latency=5.0)  # a plausible degradation signal
        elif op == "failure":
            controller.report_failure()
        else:
            controller.consume()

        assert config.min_rate - 1e-9 <= controller.rate <= config.max_rate + 1e-9


@settings(max_examples=200)
@given(_controller_and_ops())
def test_tokens_never_go_negative(data):
    config, initial_rate, ops = data
    controller = AimdController(initial_rate=initial_rate, burst=5, config=config)
    for op in ops:
        if op == "consume":
            controller.consume()
        elif op == "success":
            controller.report_success()
        elif op == "failure":
            controller.report_failure()
        assert controller.tokens >= -1e-9


@given(st.floats(min_value=0.1, max_value=100.0))
def test_set_ceiling_below_current_rate_clamps_immediately(new_ceiling):
    controller = AimdController(initial_rate=50.0, burst=3, config=ControllerConfig(max_rate=100.0))
    controller.set_ceiling(new_ceiling)
    assert controller.rate <= new_ceiling + 1e-9
    assert controller.config.max_rate == new_ceiling


@given(st.integers(min_value=1, max_value=50))
def test_wait_time_zero_iff_token_available(burst):
    controller = AimdController(initial_rate=0.001, burst=burst)
    # Exactly `burst` tokens should be immediately available with no wait.
    for _ in range(burst):
        assert controller.wait_time() == 0.0
        controller.consume()
    # The next one should require waiting (rate is tiny).
    assert controller.wait_time() > 0.0
