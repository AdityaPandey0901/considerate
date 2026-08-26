"""Tests for the `requests` Transport Adapter (C1). Uses a real local HTTP
server rather than a mocking library, both to avoid a new test-only
dependency and because it's the most convincing proof the adapter actually
intercepts `requests`' real send path, not just a hypothetical one.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

requests = pytest.importorskip("requests")

from considerate.exceptions import CircuitOpenError  # noqa: E402
from considerate.identity import AgentIdentity  # noqa: E402
from considerate.requests_adapter import ConsiderateAdapter  # noqa: E402


class _RecordingHandler(BaseHTTPRequestHandler):
    seen_headers = []
    status_for_path = {}

    def do_GET(self):
        self.seen_headers.append(dict(self.headers))
        status = self.status_for_path.get(self.path, 200)
        self.send_response(status)
        self.end_headers()
        if status < 400:
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    _RecordingHandler.seen_headers = []
    _RecordingHandler.status_for_path = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _RecordingHandler
    server.shutdown()


def test_mounted_adapter_sends_identity_header_and_returns_response(local_server):
    server, handler = local_server
    port = server.server_address[1]

    session = requests.Session()
    adapter = ConsiderateAdapter(identity=AgentIdentity(name="RequestsBot", contact="mailto:a@b.com"))
    session.mount("http://", adapter)

    response = session.get(f"http://127.0.0.1:{port}/page")
    assert response.status_code == 200
    assert response.text == "ok"

    real_request_headers = [h for h in handler.seen_headers if "considerate-agent" in {k.lower() for k in h}]
    assert real_request_headers, "no request carried the Considerate-Agent header"
    header_value = next(v for h in real_request_headers for k, v in h.items() if k.lower() == "considerate-agent")
    assert 'name="RequestsBot"' in header_value


def test_mounted_adapter_opens_circuit_on_repeated_failures(local_server):
    server, handler = local_server
    port = server.server_address[1]
    handler.status_for_path["/broken"] = 503

    session = requests.Session()
    from considerate import ConsiderateConfig
    from considerate.breaker import BreakerConfig

    config = ConsiderateConfig(breaker=BreakerConfig(consecutive_failures=2, cooldown_seconds=30))
    session.mount("http://", ConsiderateAdapter(config=config))

    session.get(f"http://127.0.0.1:{port}/broken")
    session.get(f"http://127.0.0.1:{port}/broken")

    with pytest.raises(CircuitOpenError) as excinfo:
        session.get(f"http://127.0.0.1:{port}/broken")
    assert excinfo.value.payload["status"] == "circuit_open"


def test_adapter_import_error_message_without_requests(monkeypatch):
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "requests":
            raise ImportError("simulated: requests not installed")
        return real_import(name, *args, **kwargs)

    sys.modules.pop("considerate.requests_adapter", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="pip install considerate\\[requests\\]"):
        importlib.import_module("considerate.requests_adapter")
