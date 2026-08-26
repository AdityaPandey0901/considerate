# considerate

**A drop-in rate-limiting layer for agents that fetch the web — built to protect the site being visited, not just the agent visiting it.**

```python
from considerate import ConsiderateClient, AgentIdentity

client = ConsiderateClient(
    identity=AgentIdentity(name="MyResearchBot", contact="mailto:me@example.com", intent="bulk-scrape")
)

for url in four_hundred_urls:
    response = client.get(url)   # same shape as httpx — drop it in and go
```

That's it. No config file, no tuning. `considerate` estimates how much load a
site can take, starts cautious, and only speeds up once it's confident the
site is coping — and it stops outright, rather than retrying harder, the
moment a site looks like it's struggling.

## Why

Agentic tools don't reason about capacity. Told "scrape and download these
400 documents," an LLM agent given `requests`/`httpx` and a loop will just...
loop until done. A human writing a scraper thinks about rate limits; an
agent usually doesn't, because nothing in its toolchain makes it. A few
hundred rapid requests is nothing to a CDN-fronted site — and enough to take
a self-hosted blog or a small business's server offline.

Almost everything in the current scraping-tools ecosystem (Firecrawl,
Scrapfly, proxy rotators, CAPTCHA solvers) optimizes for the *scraper's*
success — getting past blocks, staying undetected. `considerate` is the
opposite bet: a safety layer that optimizes for the *target's* wellbeing, on
by default, with zero configuration required to get baseline protection.

Think **crawl-delay, but adaptive, and default-on for agents** — not a
replacement for `robots.txt`, but a runtime layer that works even when
`robots.txt` says nothing at all, which is most of the time.

## How it works

```
Agent / Task Runner
        │
        ▼
┌────────────────────────┐
│  ConsiderateClient      │  ← drop-in replacement for httpx.Client / requests
│  (rate limiter facade)  │
└────────────┬────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
┌───────────┐   ┌───────────────┐
│ Capacity   │   │ Adaptive Rate │
│ Discovery  │──▶│  Controller   │   (AIMD — same family as TCP congestion control)
└───────────┘   └───────┬───────┘
                        ▼
               ┌─────────────────┐
               │ Circuit Breaker  │
               └────────┬─────────┘
                        ▼
                 Actual HTTP request
```

1. **Capacity discovery** (once per domain, cached 24h): check
   `/.well-known/considerate.json` for an explicit, site-published policy;
   fall back to `robots.txt`'s `Crawl-delay`; fall back to inferring a tier
   (`fragile` / `standard` / `robust`) from response headers (CDN presence)
   and time-to-first-byte on the agent's *own* first real request — no
   separate probe traffic is ever sent just to test the water.
2. **Adaptive rate control**: a token bucket seeded from the discovered
   tier. On any degradation signal (latency spike, `429`/`503`, timeout,
   connection reset) the rate is **halved immediately**. On sustained
   health it creeps back up **additively, slowly** — AIMD, the same
   asymmetry that makes TCP congestion control converge without
   oscillating between "too slow" and "took the server down."
3. **Circuit breaker**: if things clearly aren't recovering (3 consecutive
   failures, or a >20% error rate), stop sending requests to that domain
   entirely — not just slower — until a single cooldown probe succeeds.
   Every trip raises a structured `CircuitOpenError` your agent can relay
   to a human honestly:

   ```json
   {"status": "circuit_open", "domain": "small-business-site.com",
    "reason": "http_503", "retry_after": 58.2}
   ```

   That's the difference between an agent that says *"I paused scraping X
   because it looked like it was struggling"* and one that silently fails
   or hammers harder.

## The handshake

`considerate` isn't just a client-side trick — it's a small, two-way
protocol. Every request carries a `Considerate-Agent` header identifying
the agent (name, contact, intent), and any site can publish a policy file
that gives that agent (or all agents) an explicit rate, with **zero
cryptography, no signup, and no dependency on a CDN vendor**:

```json
// https://example.com/.well-known/considerate.json
{
  "version": "0.1",
  "contact": "mailto:ops@example.com",
  "default": { "requests_per_second": 0.5, "max_concurrent": 1 },
  "agents": {
    "MyResearchBot": { "requests_per_second": 2, "note": "known good actor" }
  }
}
```

See **[SPEC.md](./SPEC.md)** for the full protocol — it's designed to sit
alongside `robots.txt`, the IETF AIPREF vocabulary, and Web Bot Auth rather
than compete with them (see [SPEC.md § Relationship to other
standards](./SPEC.md#relationship-to-other-standards)).

## Install

```bash
pip install considerate                # core: sync + async httpx clients
pip install considerate[requests]      # + a requests.Session Transport Adapter
pip install considerate[playwright]    # + throttled browser navigations (browser-use, Stagehand, raw Playwright)
pip install considerate[observability] # + Prometheus metrics export
pip install considerate[yaml]          # + considerate.yaml config file support
```

## Try it on a real URL from the command line

No Python required. `pip install considerate` also installs a `considerate` CLI:

```bash
# One request: what policy did we find, what tier did we infer, what's the current rate?
considerate inspect https://example.com/some/page

# Several requests to the same URL: watch AIMD and the circuit breaker react live
considerate probe https://example.com/some/page --requests 8

# Machine-readable
considerate inspect https://example.com/ --json
```

`probe` is safe to point at a real site: it's throttled by the exact same
rate limiter this library exists to provide, so it backs itself off long
before it could do the kind of damage it's designed to prevent. `inspect`
sends exactly one request — the one you asked for — and reports what it saw:

```
considerate inspect — https://example.com/
  agent identity: name="considerate-cli", version="0.1.0", intent="research"

  request:            HTTP 200 in 94ms
  policy source:      none (inferred only)
  current rate:       2.0 req/s (soft, inferred — may climb)
  ceiling:            10.0 req/s
  circuit breaker:    closed
```

If the URL is blocked by the site's own `robots.txt`, `inspect` reports that
and exits non-zero without sending the request at all.

## Quickstart

```python
from considerate import ConsiderateClient, AgentIdentity

identity = AgentIdentity(
    name="MyResearchBot",
    version="1.0",
    contact="mailto:me@example.com",
    intent="bulk-scrape",
)

with ConsiderateClient(identity=identity) as client:
    for url in urls_to_fetch:
        response = client.get(url)
        process(response)
```

Async:

```python
from considerate import AsyncConsiderateClient, AgentIdentity

async def main():
    async with AsyncConsiderateClient(identity=AgentIdentity(name="MyResearchBot")) as client:
        response = await client.get("https://example.com")
```

Handling a paused domain honestly, instead of retrying into it:

```python
from considerate import CircuitOpenError

try:
    response = client.get(url)
except CircuitOpenError as e:
    print(f"Pausing on {e.domain}: {e.reason}. Retry after {e.retry_after:.0f}s.")
    # e.payload is the structured dict — hand it straight to your agent's
    # tool-result / user-facing report.
```

Observability — get told *why* the agent slowed down, don't just watch it happen:

```python
def on_event(event):
    print(event.kind, event.domain, event.data)

client = ConsiderateClient(identity=identity, on_event=on_event)
```

## Agent-framework integration

**Browser agents** (browser-use, Stagehand, raw Playwright) drive an actual
browser and never make an HTTP call directly — `ConsiderateClient` sees
none of that traffic. `considerate.browser` throttles real navigations
instead:

```python
from playwright.sync_api import sync_playwright
from considerate import AgentIdentity
from considerate.browser import ConsiderateBrowserPage

with sync_playwright() as p:
    page = p.chromium.launch().new_page()
    considerate_page = ConsiderateBrowserPage(page, identity=AgentIdentity(name="MyBrowserAgent"))
    considerate_page.goto("https://example.com/page-1")   # same AIMD + circuit breaker as the HTTP clients
```

See [`examples/browser_agent_example.py`](./examples/browser_agent_example.py)
for a full example (async version: `AsyncConsiderateBrowserPage`, same shape
as `AsyncConsiderateClient`). Requires `pip install considerate[playwright]`.

**Already using `requests`?** Mount a Transport Adapter instead of
switching HTTP clients — see
[`requests_adapter.py`](./src/considerate/requests_adapter.py)'s module
docstring.

**Tool-calling agents** (LangChain, CrewAI, any framework that accepts a
plain Python callable): see
[`examples/langchain_tool_example.py`](./examples/langchain_tool_example.py)
for wrapping `ConsiderateClient` as a tool, so an agent given an open-ended
"scrape N pages" task self-limits without the developer having to think
about it.

## Configuration (optional)

Zero config gives you baseline protection. For per-domain overrides:

```yaml
# considerate.yaml
default_tier: standard
overrides:
  small-business-site.com: fragile
respect_robots_txt: true
max_concurrent_per_domain: 2
circuit_breaker:
  error_threshold: 0.2
  consecutive_failures: 3
  cooldown_seconds: 60
```

```python
from considerate import ConsiderateConfig, ConsiderateClient

config = ConsiderateConfig.from_yaml("considerate.yaml")
client = ConsiderateClient(config=config)
```

## Observability and persistence

```python
client.snapshot()
# {"example.com": {"policy_source": "well-known", "current_rate_req_per_s": 1.2,
#                   "circuit_state": "closed", "hard_ceiling": True, ...}, ...}
```

A structured dump of every domain a client has touched — for a health
check, a log line, or a dashboard, without threading `on_event` through
every call site. `pip install considerate[observability]` adds a
Prometheus exporter built on the same event stream:

```python
from considerate.metrics import prometheus_event_handler
client = ConsiderateClient(on_event=prometheus_event_handler())
```

For a short-lived process (a script, a Lambda) that doesn't want to
rediscover every domain's policy on every cold start, point
`policy_cache_path` at a file — subsequent runs skip the `/.well-known`
fetch for anything still fresh:

```python
config = ConsiderateConfig(policy_cache_path="considerate_policy_cache.sqlite3")
```

## Non-goals

- **Not an anti-bot bypass tool** — the opposite goal. It won't rotate
  proxies, solve CAPTCHAs, or spoof identity for you.
- **Not a full scraping framework** — it sits underneath your existing
  HTTP client, as middleware.
- **Not malicious-scraper detection** — this is about preventing
  *accidental* harm from well-intentioned agents, not stopping bad actors.

## For website operators

You don't need to run any code to get value from this. Publish a
`/.well-known/considerate.json` (schema in [SPEC.md](./SPEC.md#2-the-site-policy-file)) — takes about five minutes — or just make sure your existing
`robots.txt` has a `Crawl-delay`. Any agent using `considerate` (and any
future library implementing the same open spec) will respect it
immediately, with no code deployed on your end.

## Status

MVP / early alpha (v0.1). The core loop — capacity discovery, AIMD control,
circuit breaker, the handshake headers — is implemented and tested. See
[SPEC.md § Roadmap](./SPEC.md#roadmap) for what's next (shared fragility
registry, MCP server wrapper, a formal standards proposal).

## License

MIT
