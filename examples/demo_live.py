"""Live demo: a real local HTTP server that gets 'unhealthy' partway through,
hit with a real ConsiderateClient over a real socket — no mocks — to show
the AIMD controller and circuit breaker actually reacting.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from considerate import AgentIdentity, CircuitOpenError, ConsiderateClient

request_count = {"n": 0}
UNHEALTHY_FROM, UNHEALTHY_UNTIL = 8, 20  # requests 8..19 come back 503


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
client = ConsiderateClient(
    identity=AgentIdentity(name="DemoBot", contact="mailto:demo@example.com"),
    on_event=lambda e: events.append(e),
)

print(f"{'req':>3}  {'result':<22}  {'rate (req/s)':<13}  events")
for i in range(1, 31):
    events.clear()
    try:
        resp = client.get("http://127.0.0.1:8765/page")
        result = f"200 ok"
    except CircuitOpenError as e:
        result = f"CIRCUIT OPEN ({e.reason}, retry {e.retry_after:.0f}s)"
    state = client._domains["127.0.0.1"]
    ev = ", ".join(f"{e.kind}" for e in events) or "-"
    print(f"{i:>3}  {result:<22}  {state.controller.rate:<13.3f}  {ev}")
    if "circuit_open" in [e.kind for e in events]:
        print("      -> agent backs off completely; sleeping past cooldown to show recovery")
        time.sleep(state.breaker.cooldown + 0.2)

client.close()
server.shutdown()
print("\ndone.")
