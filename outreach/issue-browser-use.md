<!--
DRAFT ONLY — not posted to github.com/browser-use/browser-use.
Intended as the body of a GitHub issue proposing native adoption.
-->

## Proposal: native rate-limiting/politeness for outbound navigations

browser-use gives an agent an open-ended browsing task and lets it drive
a real browser. Nothing in the current navigation path paces requests to
a given origin, which means an agent told to "check these 40 product
pages" can hit a small, self-hosted site as fast as the page loads
resolve — no `Crawl-delay` respected, no backoff on a `503`, no signal to
the agent that a site is struggling.

I ran into this building [`considerate`](https://github.com/AdityaPandey0901/considerate)
— an adaptive rate limiter + circuit breaker (AIMD, same family of
algorithm as TCP congestion control) originally built for `httpx`, now
with a Playwright wrapper (`considerate.browser.ConsiderateBrowserPage`)
that meters `page.goto()` the same way. It's a genuinely small amount of
logic: per-domain token bucket, half on failure, stop entirely after a
few consecutive failures.

Rather than asking anyone to add a dependency, I'd rather propose the
**behavior contract** as something browser-use adopts natively:

1. Before a navigation to a new origin, check `robots.txt` `Crawl-delay`
   and (optionally) `/.well-known/considerate.json` — a five-minute-setup
   JSON file some sites already publish for exactly this purpose (schema:
   [SPEC.md §4](https://github.com/AdityaPandey0901/considerate/blob/main/SPEC.md#4-the-site-policy-resource)).
2. On a `429`/`503`/timeout, halve the pacing to that origin; on
   sustained success, climb back slowly.
3. After a few consecutive failures, stop navigating to that origin and
   surface a plain-language reason the agent can relay to the user,
   instead of retrying into a struggling site or failing silently.

Happy to send a PR implementing this against `considerate`'s
`ConsiderateBrowserPage` (MIT licensed, ~150 lines, no browser-use
dependency needed if you'd rather vendor the behavior) — wanted to check
appetite for this before writing it. Is this something the maintainers
would want upstream, or is it better as a documented recipe for people to
opt into?
