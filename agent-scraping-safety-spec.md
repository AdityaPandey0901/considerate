# Agent Scraping Safety Standard — Technical Spec (MVP)

## Problem statement

Agentic tools (LLM agents with open-ended tasks like "scrape and download these 400 documents") don't reason about the *capacity* of the site they're hitting. A human writing a scraper thinks about rate limits; an agent given a goal often just... loops until done. Small/self-hosted sites have no CDN, no WAF, no elastic infra — a few hundred rapid requests can knock them offline. Current tooling (Firecrawl, Scrapfly, etc.) optimizes for the *scraper's* success (bypassing blocks, rotating proxies). Nothing in the ecosystem optimizes for the *target site's* wellbeing by default.

## Goal

A drop-in middleware/library that any agent framework can call before and during a scraping task, which:
1. Estimates target site capacity/fragility in real time.
2. Dynamically throttles request rate to stay under a safe threshold — without the agent or developer having to configure it.
3. Fails safe: backs off hard on any sign of struggle, rather than retrying aggressively.

Think of it as **"crawl-delay, but adaptive and default-on for agents"** — not a replacement for robots.txt, but a runtime safety layer that works even when robots.txt says nothing.

## Non-goals (for MVP)

- Not an anti-bot bypass tool (opposite goal).
- Not a full scraping framework — this sits *underneath* existing HTTP clients/scrapers as middleware.
- Not trying to detect *malicious* scraping — this is about accidental harm from well-intentioned agents.

## Architecture

```
Agent / Task Runner
        │
        ▼
┌───────────────────────┐
│   SafeFetch Wrapper    │  ← drop-in replacement for requests.get / fetch
│  (rate limiter facade) │
└───────────┬───────────┘
            │
    ┌───────┴────────┐
    ▼                ▼
┌─────────┐   ┌───────────────┐
│ Capacity │   │ Adaptive Rate │
│ Estimator│──▶│   Controller  │
└─────────┘   └───────┬───────┘
                       ▼
              ┌────────────────┐
              │ Circuit Breaker │
              └────────┬────────┘
                       ▼
                Actual HTTP request
```

### 1. Capacity Estimator
Runs once per new domain, cheap and fast (no burst probing):
- Check `robots.txt` for explicit `Crawl-delay` / `Request-rate` — respect it if present, done.
- Infer infra tier from response headers (`Server`, `CF-Ray`, `X-Served-By`, `Via`) — presence of Cloudflare/Fastly/Akamai headers → treat as high-capacity; absence + slow TTFB → treat as fragile.
- Sample TTFB (time-to-first-byte) on the first 1–2 requests. Anything >800ms on a simple GET is a fragility signal (small VPS, no caching layer).
- Output: a capacity tier — `fragile | standard | robust` — and a starting requests/sec ceiling per tier (configurable defaults, e.g. fragile=0.2 req/s, standard=1 req/s, robust=3 req/s).

### 2. Adaptive Rate Controller
- Token-bucket limiter seeded from the capacity tier.
- Watches live signals during the run: response latency trend, HTTP 429/503, connection resets/timeouts.
- On any degradation signal → immediately halves the rate (multiplicative decrease). On sustained health → slowly increases (additive increase), similar to TCP congestion control (AIMD). This means it starts cautious and only speeds up if the site is clearly handling it fine.

### 3. Circuit Breaker
- If error rate or latency crosses a hard threshold (e.g., 3 consecutive timeouts, or 5xx rate >20%) → **stop entirely** for that domain, not just slow down.
- Surfaces a clear, structured signal back to the agent/orchestrator: `{"status": "circuit_open", "domain": ..., "reason": ..., "retry_after": ...}` — so the agent can report back to the user honestly ("I paused scraping X because it looked like it was struggling") instead of silently failing or hammering harder.
- Cooldown + single probe request before resuming, not a full reset.

### 4. Policy layer (config, not code, for the common case)
```yaml
# safefetch.yaml
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

## MVP scope (buildable in a focused weekend/week)

1. Python package, `pip install safefetch`.
2. Sync + async wrapper around `httpx`/`requests` — same interface, so it's a near-zero-effort swap for existing scrapers/agents.
3. Capacity Estimator: header-based tier inference + TTFB sampling only (skip robots.txt parsing for v0, add in v0.1).
4. AIMD rate controller with sane defaults, no config required to get baseline protection.
5. Circuit breaker with the structured failure signal.
6. One integration example: a LangChain/simple agent tool wrapper, showing "agent given an open-ended scrape task automatically self-limits."

## Stretch (post-MVP)

- Publish capacity signals to a shared, opt-in registry so repeated agents hitting the same small site don't each have to re-discover its fragility from scratch (a kind of crowd-sourced "this site is fragile" cache — needs careful design to avoid becoming a fingerprinting/privacy issue itself).
- MCP server wrapper, so any MCP-compatible agent gets this "for free" as a tool.
- Proposal doc framed as a lightweight extension to robots.txt conventions (e.g., a `Agent-Rate-Limit` directive), pitched to a couple of framework maintainers (LangChain, CrewAI, browser-use) rather than just being a standalone library nobody adopts.

## Why this is a good weekend-scale project
- Real, current, worsening problem (AI bot traffic actively taking down sites in 2026).
- Small honest MVP is genuinely useful on day one (the httpx wrapper alone is shippable).
- Clear story: "safety layer that protects the *target*, not the *scraper*" is a differentiated, defensible niche versus the crowded "help my agent bypass blocks" tooling space.
- Naturally extensible into a standards proposal if it gets traction, without needing that outcome to justify building it.
