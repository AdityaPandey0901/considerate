# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/) once published to PyPI.
The protocol itself (SPEC.md) has its own, separate version number and
change log — a package release here does not imply a protocol version
bump, and vice versa.

## [Unreleased]

### Added
- `requests` Transport Adapter (`considerate.requests_adapter.ConsiderateAdapter`)
  — mount onto an existing `requests.Session` with zero call-site changes.
- Playwright browser-navigation wrapper (`considerate.browser`) —
  `ConsiderateBrowserPage` / `AsyncConsiderateBrowserPage`, for browser
  agents that drive an actual browser and never make an HTTP call directly.
- MCP server (`considerate-mcp`, `considerate.mcp_server`) — `fetch` and
  `considerate_status` tools for any MCP-compatible host.
- `client.snapshot()` — a structured, out-of-band view of every tracked
  domain's policy/rate/circuit state.
- Optional Prometheus metrics export (`considerate.metrics`,
  `pip install considerate[observability]`).
- Optional on-disk policy cache (`ConsiderateConfig.policy_cache_path`) so
  short-lived processes don't rediscover every domain's policy on cold start.
- CLI: `considerate policy validate` and `considerate init`;
  `considerate probe --duration`/`--out`.
- Policy file (v0.2): `disallow_paths` (now actually enforced, not just
  parsed), `crawl_windows` (time-of-day/day-of-week rate multipliers), and
  the experimental `verified_agents` field for Web Bot Auth interop.
- Published JSON Schema for the policy file (`considerate.schema`),
  backing `considerate policy validate`.
- `Retry-After` is now honored as a hard floor on the next request to a
  domain, independent of the AIMD controller — matching SPEC.md's
  original (previously unimplemented) requirement.
- CrewAI and (tested, real) LangChain tool examples.

### Fixed
- Async client used a `threading.Semaphore` for concurrency limiting,
  which could block the event loop; now uses `asyncio.Semaphore`.
- Redirects were followed inside a single httpx call, bypassing per-domain
  metering on every hop after the first; each hop now gets its own policy
  discovery, rate limit, and circuit breaker.
- Unbounded growth of tracked domains for long-running, many-host agents;
  now LRU-evicted past `max_tracked_domains`.
- No size cap on `/.well-known`/`robots.txt` fetches (a malicious/misconfigured
  host could exhaust memory); now streamed with a byte cap.
- `SitePolicy.rule_for(None)` incorrectly matched a `"*"` wildcard `agents`
  entry meant only for requests that *did* send an unrecognized name.
- Transport failures surfaced httpx's/requests' internal exception class
  names as the breaker "reason"; now a stable, library-defined vocabulary
  (`timeout`, `dns_error`, `tls_error`, `protocol_error`, `connection_error`).
- Dropped Python 3.9 support (EOL, and incompatible with a dependency
  added in this cycle) — `requires-python` is now `>=3.10`.

### Changed
- Considerate-Agent header parsing now uses a real RFC 8941 Structured
  Field Value Dictionary parser instead of a hand-rolled comma/equals split.

## [0.1.0]

Initial release: `ConsiderateClient` / `AsyncConsiderateClient` (sync +
async httpx wrappers), the `Considerate-Agent` handshake header,
`/.well-known/considerate.json` site policy discovery, `robots.txt`
`Crawl-delay` fallback, AIMD adaptive rate control, circuit breaker with
structured `CircuitOpenError` payloads, and the `considerate` CLI
(`inspect`/`probe`).
