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
Python 3.9+, `httpx` (sync + async), stdlib `urllib.robotparser` for
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
