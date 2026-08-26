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
- `hatchling` build backend requires `README.md` to exist before `pip
  install -e .` will even resolve metadata — create it before first install.
- `robotparser.can_fetch(agent, url)` accepts a full URL and internally
  strips scheme/host — no need to pass a bare path.
- `SitePolicy.rule_for(name)`: `"*"` in `agents` only applies when an agent
  *did* send a name that just wasn't listed — `rule_for(None)` (no header)
  must fall through to `default`, not `"*"`. Easy to get backwards.
