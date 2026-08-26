# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public
GitHub issue — via [GitHub's private vulnerability reporting](https://github.com/AdityaPandey0901/considerate/security/advisories/new)
on this repository, or by emailing the maintainer directly (see the
contact address on the maintainer's GitHub profile) if that's unavailable.

Include, if you can:
- The affected version(s) or commit.
- A minimal reproduction.
- What you'd expect to happen instead.

We'll acknowledge reports within a few days and aim to have a fix or a
clear mitigation plan within two weeks for anything that turns out to be
a real vulnerability, faster for anything actively exploitable.

## Scope

`considerate` is a client-side rate-limiting/circuit-breaking library. A
few things worth knowing that are **not** vulnerabilities in the usual
sense, but are relevant to how you deploy it:

- **The `Considerate-Agent` identity header is self-declared, not
  authenticated.** Nothing prevents a request from lying about its name —
  see SPEC.md §1 and §6 for why, and how this composes with Web Bot Auth
  for sites that need a stronger guarantee.
- **considerate does not protect against SSRF.** If your agent accepts
  attacker-controlled URLs and fetches them, that's a concern independent
  of this library — considerate paces *how fast* you fetch a URL, not
  *whether* you should be fetching it at all.
- **A site's `/.well-known/considerate.json` and `robots.txt` are treated
  as untrusted input**, parsed defensively (size-capped, fuzz-tested — see
  `tests/test_policy_fuzz.py`) — a malicious response is expected to
  degrade to "no policy," never to crash the calling agent or execute
  anything.

## Supported versions

Only the latest released version receives security fixes; there is no
long-term-support branch at this stage of the project.
