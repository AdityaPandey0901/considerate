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

## Docs site

`mkdocs.yml` builds an API reference (via mkdocstrings) plus the README
and SPEC.md, snippet-included so they stay a single source of truth
rather than a second copy that drifts.

```bash
pip install -e ".[dev]"
mkdocs serve          # live preview at http://127.0.0.1:8000
mkdocs build --strict # what CI runs — catches broken internal doc links
```

Not deployed anywhere yet — `mkdocs gh-deploy` publishes to a `gh-pages`
branch and GitHub Pages, both of which are a one-time, explicit decision
for a maintainer to make (it makes documentation content public at a
predictable URL), not something to run as a side effect of other changes.

## Releasing

Publishing is via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — no API token is stored anywhere. One-time setup, done once on
PyPI's side (not by CI, not by this repo):

1. At https://pypi.org/manage/project/considerate/settings/publishing/
   (or, before the project exists on PyPI yet, the equivalent "pending
   publisher" flow at https://pypi.org/manage/account/publishing/),
   register a trusted publisher with: owner `AdityaPandey0901`, repo
   `considerate`, workflow filename `release.yml`, environment `pypi`.
2. Create a `pypi` environment under this repo's Settings → Environments
   (optionally with required reviewers, for a manual approval gate on
   every release).

After that, releasing is:

```bash
# bump the version in pyproject.toml first, commit it, then:
git tag v0.2.0
git push origin v0.2.0
```

`.github/workflows/release.yml` builds the sdist/wheel, checks the tag
matches `pyproject.toml`'s version, and publishes on a match. Also update
`CHANGELOG.md`'s `[Unreleased]` section into a new dated version heading
as part of the version-bump commit, before tagging.

## Tests

No real network calls in the default test run — `httpx.MockTransport`
stands in for the target site in `tests/test_client.py`, and a real local
`http.server` stands in where MockTransport can't reach (browser tests,
the live demo). If you add a new fallback path (a new policy signal, a new
degradation heuristic), add a test for it at the same layer it lives in
(unit test in `test_policy.py`/`test_controller.py` if it's pure logic;
only add to `test_client.py` if it's genuinely about the
request-orchestration glue).

A few tests do hit the real internet, deliberately: they're marked
`@pytest.mark.network` and excluded by default (`addopts` in
`pyproject.toml`). Run them explicitly with `pytest -m network` — they run
on a weekly schedule in CI (`.github/workflows/network-smoke.yml`), not on
every push, since a network smoke test is a periodic reality check on the
mocked suite, not a substitute for it.

Property-based tests (`test_controller_properties.py`,
`test_policy_fuzz.py`) use Hypothesis to check invariants across generated
inputs rather than fixed scenarios — reach for these when adding a new
piece of stateful logic (the AIMD controller, the policy parser) rather
than only adding another example-based case.

```bash
pytest -q                              # the default suite (network tests excluded)
pytest -q -m network                   # opt-in real-internet smoke tests
pytest -q --cov=considerate --cov-report=term-missing   # coverage (floor: 75%, see pyproject.toml)
mypy src/considerate                   # type checking; the package ships py.typed
```
