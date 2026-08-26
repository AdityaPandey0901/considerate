import json
from pathlib import Path

import httpx
import pytest

from considerate.cli import build_parser, cmd_init, cmd_inspect, cmd_policy_validate, cmd_probe


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


def test_cmd_probe_duration_overrides_count(capsys):
    parser = build_parser()
    # A generous duration and a tiny -n: -n must be ignored once --duration
    # is given, per --duration's documented precedence.
    args = parser.parse_args(["probe", f"{BASE}/page", "-n", "1", "-d", "0.3s"])
    code = cmd_probe(args)
    assert code == 0
    out = capsys.readouterr().out
    # The mocked requests are effectively instant, so several should fit in
    # 300ms even with the default tier's rate limiting via burst capacity.
    assert out.count("HTTP 200") >= 2


def test_cmd_probe_writes_ndjson(tmp_path, capsys):
    out_path = tmp_path / "probe.jsonl"
    parser = build_parser()
    args = parser.parse_args(["probe", f"{BASE}/page", "-n", "2", "-o", str(out_path)])
    cmd_probe(args)

    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        row = json.loads(line)
        assert row["status_code"] == 200
        assert "rate_req_per_s" in row


# --- policy validate ---------------------------------------------------------


def test_policy_validate_accepts_the_shipped_example(capsys):
    example = Path(__file__).parent.parent / "examples" / "site_setup" / "considerate.json"
    parser = build_parser()
    args = parser.parse_args(["policy", "validate", str(example)])
    assert cmd_policy_validate(args) == 0
    out = capsys.readouterr().out
    assert "valid" in out


def test_policy_validate_rejects_a_broken_document(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"default": {"requests_per_second": -1}}))
    parser = build_parser()
    args = parser.parse_args(["policy", "validate", str(bad)])
    assert cmd_policy_validate(args) == 1
    out = capsys.readouterr().out
    assert "schema error" in out


def test_policy_validate_missing_file(capsys):
    parser = build_parser()
    args = parser.parse_args(["policy", "validate", "/nonexistent/path.json"])
    assert cmd_policy_validate(args) == 1


# --- init ---------------------------------------------------------------


def test_init_writes_a_schema_valid_file(tmp_path, capsys):
    out_path = tmp_path / "considerate.json"
    parser = build_parser()
    args = parser.parse_args(["init", "-y", "-o", str(out_path)])
    assert cmd_init(args) == 0
    assert out_path.exists()

    validate_args = parser.parse_args(["policy", "validate", str(out_path)])
    assert cmd_policy_validate(validate_args) == 0


def test_init_default_output_is_deterministic(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["init", "-y"])
    cmd_init(args)
    doc = json.loads((tmp_path / "considerate.json").read_text())
    assert doc == {
        "version": "0.2",
        "contact": "mailto:ops@example.com",
        "default": {"requests_per_second": 0.5, "max_concurrent": 1},
    }
