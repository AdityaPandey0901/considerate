import json

import httpx
import pytest

from considerate.cli import build_parser, cmd_inspect, cmd_probe


def test_common_args_work_after_subcommand():
    parser = build_parser()
    args = parser.parse_args(["inspect", "https://example.com", "--contact", "mailto:a@b.com", "--json"])
    assert args.contact == "mailto:a@b.com"
    assert args.url == "https://example.com"
    assert args.json is True


def test_probe_requires_positive_count_default():
    parser = build_parser()
    args = parser.parse_args(["probe", "https://example.com"])
    assert args.requests == 8


HOST = "testserver"
BASE = f"https://{HOST}"


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/.well-known/considerate.json", "/robots.txt"):
        return httpx.Response(404)
    return httpx.Response(200, content=b"ok")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    # Every considerate.client.httpx.Client() call in these CLI tests should
    # go through MockTransport, never a real socket.
    import httpx as httpx_module

    real_client = httpx_module.Client

    def patched(*args, **kwargs):
        kwargs.setdefault("transport", httpx_module.MockTransport(_handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx_module, "Client", patched)


def test_cmd_inspect_json_output(capsys):
    parser = build_parser()
    args = parser.parse_args(["inspect", f"{BASE}/page", "--json"])
    code = cmd_inspect(args)
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["domain"] == HOST
    assert data["policy_source"] == "none (inferred only)"


def test_cmd_probe_runs_requested_number_of_requests(capsys):
    parser = build_parser()
    args = parser.parse_args(["probe", f"{BASE}/page", "-n", "3"])
    code = cmd_probe(args)
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("HTTP 200") == 3
