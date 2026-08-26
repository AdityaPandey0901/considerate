# Introducing considerate: a safety layer for the side of scraping nobody's building for

Ask an LLM agent to "scrape and download these 400 documents" and it will
do exactly that — as fast as it possibly can. A human writing the same
scraper almost always adds a delay between requests, out of habit if
nothing else. An agent given `requests.get` in a loop has no such habit.
It just loops until done.

That gap is starting to matter. A growing share of the traffic hitting the
open web now comes from agents, not scripts a person tuned by hand. Most
of the sites on the other end of that traffic are not Cloudflare-fronted
platforms with elastic infrastructure — they're a small business's
WordPress install, a hobbyist's blog, a university department's static
site, running on hardware sized for a few hundred visitors a day. A
few hundred rapid, well-intentioned requests from an agent that "just
wants the data" is enough to take one of those offline.

Almost everything built for agent-driven scraping in the last two years
optimizes for the *scraper's* success: bypassing blocks, rotating
proxies, solving CAPTCHAs, staying undetected. That's a reasonable thing
to build, but it means nothing in the ecosystem is optimizing for the
*target's* wellbeing by default. We built `considerate` to be that thing.

## What it actually does

```python
from considerate import ConsiderateClient, AgentIdentity

client = ConsiderateClient(
    identity=AgentIdentity(name="MyResearchBot", contact="mailto:me@example.com")
)

for url in four_hundred_urls:
    response = client.get(url)
```

Same shape as `httpx`. Zero configuration. And underneath, on every new
domain, it:

1. **Checks whether the site told it what to do.** First a
   `/.well-known/considerate.json` policy file (see below), then
   `robots.txt`'s `Crawl-delay`. If either exists, that's the ceiling,
   full stop — no exploring past it.
2. **If the site said nothing, infers a starting point** from signals it
   already has on hand — is there a CDN in front of this origin, how slow
   was time-to-first-byte — with zero dedicated probe traffic. Just the
   first real request the agent was going to make anyway.
3. **Adapts like TCP does.** A token bucket per domain, additive increase
   on sustained health, multiplicative decrease at the first sign of
   trouble — a timeout, a `429`, a `503`, a latency spike. Start cautious,
   speed up slowly, back off hard and immediately.
4. **Stops entirely, not just slower, when a site is clearly struggling.**
   Three consecutive failures or a sustained error rate opens a circuit
   breaker for that domain. No more requests go out until a single
   cooldown probe succeeds. And it hands back something an agent can
   actually use:

   ```json
   {"status": "circuit_open", "domain": "small-business-site.com",
    "reason": "http_503", "retry_after": 58.2}
   ```

   That's the difference between an agent that tells its user *"I paused
   scraping this site — it looked like it was struggling"* and one that
   either fails silently or, worse, retries into the failure harder.

## Why this doesn't already exist

We looked. There is real, active standards work adjacent to this problem
right now:

- **IETF's AIPREF working group** is standardizing how a site expresses
  *permission* — may this content train a model, may it be indexed —
  carried in `robots.txt` and HTTP headers. That's a different question
  than capacity; a site can say yes to training and still get knocked over
  by the crawl itself.
- **IETF's Web Bot Auth working group** is standardizing cryptographic bot
  *identity* — signed HTTP messages, a JWKS-backed metadata document (the
  "Signature Agent Card") that already has `rate-control` and
  `rate-expectation` fields. It's the right long-term answer for
  large-scale verified crawlers, and it's exactly the kind of
  infrastructure — key management, a registry, signature verification —
  that's out of reach for the sites most at risk here.
- **Cloudflare** shipped Content Signals and Pay Per Crawl, extending
  `robots.txt` with reuse-tier and monetization semantics, enforced at
  their edge. Powerful, but scoped to sites on that platform.
- A handful of **`agents.txt` proposals** are converging on capability
  discovery — what transactions, what MCP endpoints, what auth a site
  supports for agents. Useful, and answering yet another different
  question than "how fast can I hit you right now."

None of it answers *"how much load can this specific origin take, right
now, and what should an agent do the moment that's exceeded"* — for the
overwhelming majority of the web that isn't behind an enterprise CDN and
isn't going to stand up a JWKS endpoint. And on the client side: no
popular browser-agent framework (browser-use, Stagehand, Skyvern) ships
politeness by default. It's a real gap, not a crowded space we're adding
one more entrant to.

## The two-way part

A pure client-side heuristic can only ever guess. So `considerate` is
also a small protocol — the part we'd genuinely like other people's agents
and other people's sites to speak, not just this one library.

**The agent side:** every request carries a `Considerate-Agent` header —
name, version, contact, intent. It costs nothing to send, and it's the
one thing that turns "block this IP" into "email this person."

**The site side:** any site — no CDN, no signup, no cryptography — can
publish, in about five minutes:

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

A site that recognizes a specific agent's name can give it a higher rate
than an anonymous default. An agent that gets that policy back is required
— not just invited — to treat it as a ceiling. The full grammar, the
fallback order, and exactly how it's meant to compose with `robots.txt`,
AIPREF, and Web Bot Auth is in [SPEC.md](../SPEC.md), written as a
protocol document on purpose: we want a Node scraper, a Go crawler, and a
future browser-use plugin to be able to implement either side of this
without depending on our Python package at all.

## Try it

```bash
pip install considerate
```

```python
from considerate import ConsiderateClient, AgentIdentity, CircuitOpenError

client = ConsiderateClient(
    identity=AgentIdentity(name="MyBot", contact="mailto:me@example.com", intent="bulk-scrape")
)

try:
    response = client.get("https://example.com/page")
except CircuitOpenError as e:
    print(f"Backing off {e.domain}: {e.reason}, retry after {e.retry_after:.0f}s")
```

If you run a site and want to tell agents what you can handle, drop a
[`considerate.json`](../examples/site_setup/considerate.json) at
`/.well-known/` — or just make sure your `robots.txt` has a `Crawl-delay`,
which `considerate` (and, going forward, we hope, other implementations of
this spec) will respect with no code deployed on your end.

If you maintain an agent framework — LangChain, CrewAI, browser-use,
Skyvern — we'd rather this behavior end up built into your HTTP layer
natively than live forever as a wrapper people have to remember to add.
[SPEC.md](../SPEC.md) is written to make that adoption path as small as
possible. Open an issue, or just implement §1's header + behavior contract
directly — we're not precious about the code being the point. The point is
fewer small sites going down because an agent didn't know it was possible
to ask.
