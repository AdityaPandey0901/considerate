# considerate

## Project Context
Respectful, adaptive rate-limiting library for LLM/browser agents that fetch
the web — protects the *target site*, not the scraper. Implements (and
defines, in SPEC.md) a small two-way protocol: agents send a
`Considerate-Agent` identity header and are required to back off hard on
degradation signals; sites can optionally publish
`/.well-known/considerate.json` to declare explicit capacity. Originated from
`../agent-scraping-safety-spec.md`.

## Tech Stack
Python 3.10+ (bumped from 3.9 once CI actually ran on 3.9 and broke — see
Learnings below), `httpx` (sync + async), stdlib `urllib.robotparser` for
robots.txt. Optional `pyyaml` for config files. Tests: `pytest` +
`pytest-asyncio`, no real network calls (`httpx.MockTransport`). Packaged
with hatchling. Own `.venv/` in this directory — do not use the parent
`AI_Safety_Projects/.venv`.

## Key Conventions
- `policy.py`, `controller.py`, `breaker.py` are pure/I/O-free and unit
  tested directly; `client.py` is the only place doing real network I/O.
- `_domain.py`'s `DomainState` is the single per-domain object shared by
  sync/async clients — keep sync and async client logic symmetric when
  editing one.
- An explicit site policy (`well-known` or `robots.txt Crawl-delay`) is a
  **hard ceiling** (`DomainState.hard_ceiling = True`); an inferred tier is
  a **soft starting point** AIMD may climb above. Don't blur this distinction.
- SPEC.md is the protocol; treat it as the source of truth for wire format,
  README/code as the reference implementation of it.

## Learnings & Notes
- **CI had been red since section A and nobody (i.e. me) checked** — local
  venvs were always 3.10+, so `X | Y` union syntax in test files (no
  `from __future__ import annotations` needed there since src/ always had
  it, but tests/ didn't) silently broke Python 3.9 in CI for four straight
  pushes. Also `mcp>=2.0` (added in section E) has no 3.9 wheel at all, so
  `pip install -e ".[dev]"` would have failed outright on 3.9 regardless.
  Python 3.9 reached EOL in Oct 2025 anyway, so the fix was dropping it
  (`requires-python = ">=3.10"`), not chasing compatibility with an
  unsupported interpreter. Lesson: `gh run list` after every push that
  touches CI-relevant files (deps, Python version matrix, new test files)
  — passing tests locally is not the same claim as passing CI.
- `requests.adapters.HTTPAdapter.__init__` unconditionally sets
  `self.config = {}` for its own urllib3 pool/proxy bookkeeping — a real
  attribute name collision if a subclass (`ConsiderateAdapter`) also wants
  `self.config`. Store considerate's own config under a different name
  (`considerate_config`) in anything that subclasses `HTTPAdapter`.
- Playwright's async driver bootstrap conflicts with pytest-asyncio's
  event-loop management (`Runner.run()`/`asyncio.run() cannot be called
  from a running event loop`) as soon as another async test has already
  run earlier in the same pytest session — reproduces intermittently,
  depends on test order and pytest-asyncio version, not fixable by a
  version pin alone. Run any `playwright.async_api` test in a genuinely
  separate process (see `tests/_async_browser_subprocess.py` +
  `subprocess.run`), not as an in-process `pytest.mark.asyncio` coroutine.
- `_SharedLogic` (client.py) is reused by three integrations now (httpx
  clients, requests adapter, browser wrapper) via mixin inheritance —
  before adding a fourth, check whether the method you need touches
  `self.config` (collision risk, see above) before assuming it's reusable
  as-is.
- SPEC.md's protocol version (now 0.2) and the package's own semver
  (`_version.py`, still 0.1.0) are tracked independently on purpose — the
  wire format and the Python package don't have to release in lockstep.
- `DomainState.base_ceiling` vs. `controller.config.max_rate`: base_ceiling
  is what policy/inference set; `max_rate` is base_ceiling × the current
  crawl_windows multiplier, recomputed every request via
  `refresh_effective_ceiling()`. Never set `controller.config.max_rate`
  directly outside that method or a window's effect gets silently baked in
  permanently.
- `cli.py` (`considerate inspect|probe <url>`): common flags (`--json`,
  `--contact`, `--agent-name`) are defined **only on the subparsers**, not
  the top-level parser — argparse's `_SubParsersAction` merges the whole
  subnamespace back onto the parent on invocation, which silently clobbers
  a same-dest flag given *before* the subcommand with the subparser's
  default. Flags go after the subcommand/URL only.
- `hatchling` build backend requires `README.md` to exist before `pip
  install -e .` will even resolve metadata — create it before first install.
- `robotparser.can_fetch(agent, url)` accepts a full URL and internally
  strips scheme/host — no need to pass a bare path.
- `SitePolicy.rule_for(name)`: `"*"` in `agents` only applies when an agent
  *did* send a name that just wasn't listed — `rule_for(None)` (no header)
  must fall through to `default`, not `"*"`. Easy to get backwards.
