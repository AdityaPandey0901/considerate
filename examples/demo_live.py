"""Live demo: a real local HTTP server that gets 'unhealthy' partway through,
hit with a real ConsiderateClient over a real socket — no mocks — to show
the AIMD controller and circuit breaker actually reacting.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from considerate import AgentIdentity, CircuitOpenError, ConsiderateClient, ConsiderateConfig
from considerate.breaker import BreakerConfig
from considerate.controller import ControllerConfig

request_count = {"n": 0}
# Exactly 3 bad responses -> exactly one circuit trip, then the server is
# healthy again by the time the single cooldown probe fires (a real
# implementation doesn't know the server will recover; this window is
# shaped so the demo shows one clean trip+recovery cycle instead of the
# breaker's real (and correct) exponential-cooldown behavior on a probe
# that keeps failing, which you'd see if UNHEALTHY_UNTIL were higher).
UNHEALTHY_FROM, UNHEALTHY_UNTIL = 8, 11


class FlakyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request_count["n"] += 1
        n = request_count["n"]
        if UNHEALTHY_FROM <= n < UNHEALTHY_UNTIL:
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # silence default access log


server = ThreadingHTTPServer(("127.0.0.1", 8765), FlakyHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()

events = []
# NOTE: cooldown_seconds and the controller's climb-back speed are tuned
# down here purely so this demo finishes in well under a minute of real
# wall-clock waiting. The library's actual defaults (60s cooldown, a
# 10-success streak before climbing) are far more conservative — see
# considerate/config.py. What's NOT tuned down: the 0.5x multiplicative
# drop on failure, and the AIMD shape itself.
config = ConsiderateConfig(
    breaker=BreakerConfig(consecutive_failures=3, cooldown_seconds=2.0),
    controller=ControllerConfig(success_streak_for_increase=2, additive_step=0.3),
)
client = ConsiderateClient(
    identity=AgentIdentity(name="DemoBot", contact="mailto:demo@example.com"),
    config=config,
    on_event=lambda e: events.append(e),
)


def tool_result_for(url: str) -> str:
    """The exact string shape a `@tool`-wrapped fetch (see
    examples/langchain_tool_example.py) returns as its tool_result — plain
    text, because that's what an LLM agent actually reasons over. It never
    sees the CircuitOpenError object or its payload dict directly.
    """
    try:
        r = client.get(url)
        return f"Fetched {url} successfully ({len(r.content)} bytes)."
    except CircuitOpenError as e:
        return (
            f"PAUSED: stopped fetching from {e.domain} because it looked like it was "
            f"struggling ({e.reason}). Safe to retry after {e.retry_after:.0f}s. Tell the "
            "user this site is being skipped for now rather than retrying immediately."
        )


start = time.monotonic()
captured_circuit_open_tool_result = None
print(f"{'t':>5}  {'req':>3}  {'result':<22}  {'rate (req/s)':<13}  events")
for i in range(1, 19):
    events.clear()
    try:
        resp = client.get("http://127.0.0.1:8765/page")
        result = "200 ok"
    except CircuitOpenError as e:
        result = f"CIRCUIT OPEN ({e.reason}, retry {e.retry_after:.0f}s)"
        # Capture what an agent's tool call would have seen at this exact
        # moment (the circuit is open right now) rather than replaying it
        # later, by which point the demo server has recovered.
        captured_circuit_open_tool_result = tool_result_for("http://127.0.0.1:8765/page")
    state = client._domains["127.0.0.1"]
    ev = ", ".join(f"{e.kind}" for e in events) or "-"
    elapsed = time.monotonic() - start
    print(f"{elapsed:>4.1f}s  {i:>3}  {result:<22}  {state.controller.rate:<13.3f}  {ev}")
    if "circuit_open" in [e.kind for e in events]:
        print("       -> agent backs off completely; sleeping past cooldown to show recovery")
        time.sleep(state.breaker.cooldown + 0.2)

print("\n--- what actually landed in the agent's tool-result / reasoning trace ---\n")
print(f'tool_result = "{captured_circuit_open_tool_result}"')
print(
    "\n-> an agent conditioned on this text will reason something like:\n"
    '   "The tool told me 127.0.0.1 is struggling and to wait a couple seconds.\n'
    '    I should stop fetching from it now and tell the user, instead of retrying."\n'
    "   That sentence is possible because the CircuitOpenError's structured\n"
    "   payload {status, domain, reason, retry_after} was turned into plain\n"
    "   language BEFORE it reached the model — the model never sees a stack\n"
    "   trace or a raw exception, only the tool_result string above."
)

client.close()
server.shutdown()
print("\ndone.")
