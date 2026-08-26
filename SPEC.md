# The Considerate Protocol — v0.1

Status: **Draft / MVP.** This describes what the `considerate` Python
library implements today, written as a protocol rather than an API so any
other agent framework, language, or site can implement either side without
depending on this repo. Feedback and counter-proposals welcome — see
[Roadmap](#roadmap).

## 0. Motivation

`robots.txt` answers *"which paths may a bot fetch."* Nothing in wide use
answers *"how fast may a bot fetch them, right now, given this site's
actual capacity"* — and that second question is the one currently taking
small, self-hosted sites offline under agentic traffic. `Crawl-delay` gets
partway there but is a static, single number, unauthenticated, largely
ignored by major crawlers, and gives a site no way to say "here's a higher
rate for agents I recognize" or to react to *live* struggle.

The Considerate Protocol is two small, complementary pieces:

1. **A site policy file** — a static, structured capacity declaration a
   site publishes once, for any agent to read.
2. **An agent identity header** — a lightweight self-identification an
   agent sends on every request, plus a mandatory behavioral contract for
   how it must react to standard HTTP signals (`429`, `503`,
   `Retry-After`) it gets back.

Neither requires cryptography, a signup, or a CDN vendor. That's
deliberate: the goal is a floor any site can adopt in five minutes, not a
ceiling for the largest crawler operators (see [§6](#6-relationship-to-other-standards)
for where those heavier mechanisms fit instead).

## 1. The agent identity header

Every request an implementing agent sends **SHOULD** include:

```
Considerate-Agent: name="MyResearchBot", version="1.0", intent="bulk-scrape", contact="mailto:me@example.com"
```

Grammar (informal; a deliberate subset of RFC 8941 Structured Field
syntax, so it stays parseable without a dedicated library in any language):

```
considerate-agent = member *( OWS "," OWS member )
member            = key "=" DQUOTE value DQUOTE
key               = "name" / "version" / "intent" / "contact" / token
```

Fields:

| Field     | Required | Meaning |
|-----------|----------|---------|
| `name`    | yes      | Stable identifier for the agent. This is the key a site's policy file matches against — keep it constant across versions of the same agent. |
| `version` | no       | Free-form version string. |
| `intent`  | no       | One of `browse`, `bulk-scrape`, `monitor`, `research`, or a custom value. Informational only — sites may use it for logging or future policy, agents don't need to enforce anything based on their own declared intent. |
| `contact` | strongly recommended | A `mailto:` or `https://` URL a human at the site can use to reach a human responsible for this agent's traffic. This is the single highest-leverage field: it turns "block this IP" into "email this person," which is a better outcome for everyone. |

An agent identity is a courtesy, not an authentication mechanism — nothing
stops a bad actor from lying in this header, exactly as nothing stops one
from lying in `User-Agent` today. It composes with (and is superseded in
trust by) cryptographic bot identity for sites that need that guarantee —
see [§6](#6-relationship-to-other-standards).

### 1.1 Mandatory agent behavior

Implementing this half of the protocol without the rest is not compliant.
An agent that sends the header but ignores the signals below is worse than
one that sends nothing, because it creates a false impression of good
faith. A compliant agent **MUST**:

- Treat any `429` or `503` response, or a sustained latency increase, as
  an immediate signal to at least halve its request rate to that origin.
- Honor a `Retry-After` header as a floor, not a suggestion.
- Stop sending requests to an origin entirely — not just slower — after a
  small number of consecutive failures or a sustained high error rate,
  and require an explicit successful "probe" before resuming.
- Never re-issue a failed request into an origin that is currently in
  this stopped state as a retry.

## 2. The site policy file

A site publishes, at `/.well-known/considerate.json`:

```jsonc
{
  "version": "0.1",
  "contact": "mailto:ops@example.com",
  "default": {
    "requests_per_second": 0.5,
    "max_concurrent": 1,
    "burst": 2
  },
  "agents": {
    "MyResearchBot": {
      "requests_per_second": 2,
      "note": "Known good actor, contacted us directly."
    },
    "*": {
      "requests_per_second": 0.2,
      "note": "Unrecognized agents get a conservative default even below `default`."
    }
  }
}
```

### Field reference

| Field | Type | Meaning |
|---|---|---|
| `version` | string | Protocol version this document targets. `"0.1"` for this draft. |
| `contact` | string | `mailto:`/`https:` URL for the site's operator. |
| `default` | object | The rate rule applied when no request identifies itself, or identifies with a `name` not present in `agents`. |
| `agents` | object | Map from an agent's declared `Considerate-Agent` `name` to a rate rule that overrides `default` for that agent specifically. A `"*"` key applies to any request that *does* send an identity header but doesn't match a more specific entry — distinct from `default`, which is the fallback for no header at all. |

**Rate rule object:**

| Field | Type | Meaning |
|---|---|---|
| `requests_per_second` | number | Ceiling. An implementing agent's adaptive controller **MUST NOT** exceed this, though it may throttle below it in response to live signals. |
| `max_concurrent` | integer | Max simultaneous in-flight requests to this origin. |
| `burst` | integer | Token bucket burst capacity. |
| `tier` | string | One of `fragile` \| `standard` \| `robust`, an alternative to specifying `requests_per_second` directly — lets a site say "treat me like a small site" without picking a number. |
| `note` | string | Free-form, surfaced in agent logs/events; no protocol meaning. |

A missing policy file, or a `4xx`/`5xx` response fetching it, is not an
error — it means "no explicit policy," and an implementing agent falls
back to §3.

An agent **MUST** treat an explicit `requests_per_second` from this file
as a hard ceiling — never something to cautiously explore past, unlike an
inferred rate (§3). If the site said 0.5 req/s, that's the site telling
you its actual limit, not a starting guess.

### 2.1 Discovery and caching

- Fetched via a normal `GET` over HTTPS (falling back to HTTP only if
  HTTPS is unavailable), once per origin, cached for **24 hours** by
  default.
- Fetching this file is exempt from the rate controller in §4 — it's a
  single small request, the same category of traffic as fetching
  `robots.txt`, which every serious crawler already does per-origin.
- Malformed JSON, or a document missing required structure, **MUST** be
  treated identically to "absent" (fall back to §3) — a strict parser
  here just discourages sites from publishing anything at all. Unknown
  fields **MUST** be ignored, for forward compatibility.

## 3. Fallback capacity inference

When no policy file and no `robots.txt` `Crawl-delay` apply, an agent
infers a starting tier from signals available on the very first real
request it was going to make anyway — **no dedicated probe traffic**:

| Signal | Inference |
|---|---|
| Response headers indicate a CDN/edge in front of the origin (e.g. `CF-Ray`, `X-Served-By`, `Via`, a recognized `Server` token) | `robust` |
| Time-to-first-byte on a simple `GET` exceeds ~800ms with no CDN signal | `fragile` |
| Neither | `standard` |

Suggested starting rates and ceilings (implementations may tune these; a
future version of this spec may standardize the defaults themselves):

| Tier | Starting rate | Ceiling (AIMD may grow up to) |
|---|---|---|
| `fragile` | 0.2 req/s | 0.5 req/s |
| `standard` | 1 req/s | 3 req/s |
| `robust` | 3 req/s | 10 req/s |

Unlike an explicit site policy, an inferred ceiling is a *soft* starting
point — the whole point of §4's AIMD control is to cautiously explore
upward from it when a site is clearly coping fine.

## 4. The adaptive rate controller (AIMD)

A token bucket per origin, seeded from §2 or §3:

- **On any degradation signal** (timeout, connection reset, `429`,
  `503`, `5xx`, or latency exceeding ~2.5× the origin's rolling baseline)
  → multiply the rate by **0.5**, immediately.
- **On sustained success** (a configurable streak of clean, on-latency
  responses — 10 by default) → add a small fixed increment to the rate.
- The rate never exceeds the ceiling from §2/§3, and never drops below a
  small floor (agents should never fully stop making forward progress
  outside of an open circuit breaker, which is a distinct state — §5).

This is the same asymmetric-response shape as TCP congestion control, for
the same reason: additive increase / multiplicative decrease converges to
"as fast as is currently safe" without the oscillation a symmetric
increase/decrease would cause.

## 5. The circuit breaker

Distinct from, and layered above, §4. The controller answers "how fast is
safe"; the breaker answers "has this origin stopped coping at all."

- Opens (stop sending anything to this origin) on **3 consecutive
  failures**, or an **error rate ≥ 20%** over a rolling window, whichever
  comes first. Both thresholds are configurable.
- While open, an agent **MUST NOT** send further requests to the origin
  until a cooldown elapses (default 60s, doubling on repeated trips, up
  to a cap).
- After cooldown, exactly **one probe request** is allowed through
  (half-open state). Success closes the circuit and resets backoff;
  failure reopens it with a longer cooldown.
- Opening the circuit **MUST** surface a structured, machine-readable
  signal to whatever is driving the agent:

  ```json
  {"status": "circuit_open", "domain": "small-business-site.com", "reason": "http_503", "retry_after": 58.2}
  ```

  so an agent can report honestly to its user ("I paused scraping X
  because it looked like it was struggling") instead of silently failing
  or retrying into the failure.

## 6. Relationship to other standards

This protocol is deliberately narrow. It is not trying to replace, and is
designed to be adopted alongside:

- **`robots.txt` (RFC 9309) / `Crawl-delay`** — scope (*which paths*) and
  a static per-crawler delay. The Considerate Protocol treats
  `Crawl-delay` as one of its capacity-discovery inputs (§3) and always
  respects `Disallow` before anything else applies.
- **IETF AIPREF** (`draft-ietf-aipref-vocab`) — expresses *permission*
  (may this content be used for training/search), not *capacity*. A site
  can publish both an AIPREF policy and a `considerate.json` with no
  conflict; they answer different questions.
- **IETF Web Bot Auth** (`draft-meunier-webbotauth-registry`) — solves
  *cryptographic identity*: proving an agent really is who its header
  claims, via signed HTTP messages and a JWKS-backed "Signature Agent
  Card." That card format already has `rate-control`/`rate-expectation`
  fields, and a site that wants verified-identity rate policy should use
  it. The Considerate Protocol is intentionally the on-ramp below that:
  no keys, no registry, adoptable by a static-site blog in five minutes.
  A natural v0.2 direction is letting a `considerate.json` `agents` entry
  key off a Web Bot Auth-verified identity instead of (or in addition to)
  the self-declared `name` — see Roadmap.
- **`agents.txt`** (agents-txt.com and related proposals) — capability
  discovery for transactional agent interactions (payments, MCP
  endpoints, A2A cards). Complementary; a site can point to both files
  independently.
- **Cloudflare Content Signals / Pay Per Crawl** — reuse-permission and
  monetization, enforced at the CDN edge for sites on that platform. The
  Considerate Protocol works for the much larger set of sites that aren't
  behind an enterprise CDN, which is precisely the population most at
  risk from agentic load.

## Roadmap

Not in v0.1, listed here so the scope of the current implementation is
explicit rather than implied:

- **Shared fragility registry** — an opt-in, crowd-sourced cache so
  repeated agents hitting the same small site don't each rediscover its
  fragility independently. Needs careful design so the registry itself
  doesn't become a fingerprinting or privacy surface (e.g., a domain
  should not be able to tell *which* agents queried it).
- **MCP server wrapper** — expose the client as an MCP tool so any
  MCP-compatible agent gets this behavior with no code, following the
  same "capability, not config" philosophy as the rest of the protocol.
- **Web Bot Auth interop** — verified-identity-keyed policy entries, per
  §6.
- **`disallow_paths` / crawl-window fields** in the policy file (drafted
  in early versions of this doc, deferred to keep v0.1's parser minimal).
- Submission of this document, revised with implementer feedback, as an
  Internet-Draft, and outreach to agent-framework maintainers (LangChain,
  CrewAI, browser-use) to adopt the header/behavior contract in §1
  natively rather than requiring an explicit wrapper.

## Change log

- **v0.1** (2026) — initial draft: `Considerate-Agent` header,
  `/.well-known/considerate.json`, AIMD controller, circuit breaker.
