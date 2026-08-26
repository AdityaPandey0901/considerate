# Contributing

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -q
```

## Where things live

- `src/considerate/policy.py` — parsing for the site-side policy file and
  `robots.txt` `Crawl-delay`. Pure functions, no I/O — keep it that way, it's
  what makes this trivially testable.
- `src/considerate/controller.py` — the AIMD token bucket. One instance per
  domain.
- `src/considerate/breaker.py` — the circuit breaker. One instance per
  domain, layered above the controller.
- `src/considerate/_domain.py` — glues policy + controller + breaker into
  one `DomainState`, and does the tier-inference heuristic on an
  uncalibrated domain's first request.
- `src/considerate/client.py` — the actual `httpx`-wrapping sync/async
  clients. This is the only place that does real network I/O; almost
  everything else is a pure data transform on purpose.

## Protocol changes

`SPEC.md` describes the wire protocol (the `Considerate-Agent` header and
`/.well-known/considerate.json`) independently of this implementation — it's
meant to be adoptable by other languages/frameworks. If a change affects the
wire format, update `SPEC.md` first, bump its version, and treat the Python
implementation as the reference client, not the source of truth.

## Tests

No network calls in the test suite — `httpx.MockTransport` stands in for
the target site in `tests/test_client.py`. If you add a new fallback path
(a new policy signal, a new degradation heuristic), add a test for it at
the same layer it lives in (unit test in `test_policy.py`/`test_controller.py`
if it's pure logic; only add to `test_client.py` if it's genuinely about the
request-orchestration glue).
