from considerate.controller import AimdController, ControllerConfig


def test_initial_tokens_allow_immediate_burst():
    controller = AimdController(initial_rate=1.0, burst=3)
    assert controller.wait_time() == 0.0
    controller.consume()
    controller.consume()
    controller.consume()
    # Bucket now empty; a 4th token is not immediately available.
    assert controller.wait_time() > 0.0


def test_failure_halves_rate():
    controller = AimdController(initial_rate=2.0, burst=3)
    controller.report_failure()
    assert controller.rate == 1.0
    controller.report_failure()
    assert controller.rate == 0.5


def test_rate_never_drops_below_floor():
    config = ControllerConfig(min_rate=0.1, decrease_factor=0.5)
    controller = AimdController(initial_rate=0.15, burst=1, config=config)
    for _ in range(10):
        controller.report_failure()
    assert controller.rate == config.min_rate


def test_success_streak_increases_rate_additively():
    config = ControllerConfig(success_streak_for_increase=3, additive_step=0.2, max_rate=5.0)
    controller = AimdController(initial_rate=1.0, burst=3, config=config)
    for _ in range(3):
        controller.report_success()
    assert controller.rate == 1.2


def test_rate_never_exceeds_ceiling():
    config = ControllerConfig(success_streak_for_increase=1, additive_step=1.0, max_rate=2.0)
    controller = AimdController(initial_rate=1.5, burst=3, config=config)
    for _ in range(10):
        controller.report_success()
    assert controller.rate == 2.0


def test_latency_degradation_triggers_decrease_without_explicit_failure():
    config = ControllerConfig(latency_degradation_multiplier=2.0)
    controller = AimdController(initial_rate=1.0, burst=3, config=config)
    controller.report_success(latency=0.4)  # sets baseline
    before = controller.rate
    controller.report_success(latency=2.0)  # >> baseline * multiplier
    assert controller.rate < before


def test_set_ceiling_clamps_current_rate_down():
    controller = AimdController(initial_rate=5.0, burst=3)
    controller.set_ceiling(1.0)
    assert controller.rate == 1.0
