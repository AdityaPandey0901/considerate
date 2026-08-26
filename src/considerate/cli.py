"""`considerate` command-line tool: point it at a real URL and see exactly
what the library sees and does, without writing any Python.

    considerate inspect https://example.com/some/page
    considerate probe   https://example.com/some/page --requests 8

`inspect` makes exactly one request (the one you gave it) and reports what
policy was discovered and what tier was inferred from it — it does not send
extra probe traffic, per the library's own "no dedicated probe requests"
principle.

`probe` repeats that same URL a few times so you can watch the AIMD
controller and circuit breaker react live. It is safe to point at a real
site by construction: it's still going through the same rate limiter this
whole library exists to provide, so it will throttle itself down long
before it could do the kind of damage it's designed to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from ._version import __version__
from .client import ConsiderateClient
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity


def _client(args: argparse.Namespace) -> ConsiderateClient:
    identity = AgentIdentity(
        name=args.agent_name,
        version=__version__,
        contact=args.contact,
        intent="research" if args.command == "inspect" else "monitor",
    )
    return ConsiderateClient(identity=identity, timeout=10.0)


def _domain_report(client: ConsiderateClient, host: str) -> dict:
    state = client._domains[host]
    rule = state.policy.rule_for(client.identity.name) if state.policy else None
    return {
        "domain": host,
        "policy_source": state.policy.source if state.policy else "none (inferred only)",
        "policy_contact": state.policy.contact if state.policy else None,
        "hard_ceiling": state.hard_ceiling,
        "current_rate_req_per_s": round(state.controller.rate, 4),
        "ceiling_req_per_s": round(state.controller.config.max_rate, 4),
        "max_concurrent": state.max_concurrent,
        "declared_rate_for_this_agent": rule.requests_per_second if rule else None,
        "circuit_state": state.breaker.state.value,
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    client = _client(args)
    host = httpx.URL(args.url).host
    if not args.json:
        print(f"considerate inspect — {args.url}")
        print(f"  agent identity: {client.identity.to_header()}\n")

    start = time.monotonic()
    try:
        response = client.get(args.url)
        latency = time.monotonic() - start
        outcome = f"HTTP {response.status_code} in {latency * 1000:.0f}ms"
    except (DisallowedError, CircuitOpenError, httpx.HTTPError) as e:
        client.close()
        payload = getattr(e, "payload", None) or {"status": "error", "message": str(e)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"  {payload.get('status', 'error').upper()}: {e}")
        return 1

    report = _domain_report(client, host)
    client.close()

    if args.json:
        print(json.dumps({"request": outcome, **report}, indent=2))
        return 0

    print(f"  request:            {outcome}")
    print(f"  policy source:      {report['policy_source']}")
    if report["policy_contact"]:
        print(f"  site contact:       {report['policy_contact']}")
    print(f"  current rate:       {report['current_rate_req_per_s']} req/s"
          f"{' (hard ceiling — set by the site)' if report['hard_ceiling'] else ' (soft, inferred — may climb)'}")
    print(f"  ceiling:            {report['ceiling_req_per_s']} req/s")
    if report["declared_rate_for_this_agent"] is not None:
        print(f"  rate declared for '{client.identity.name}': {report['declared_rate_for_this_agent']} req/s")
    print(f"  circuit breaker:    {report['circuit_state']}")

    if report["policy_source"] == "none (inferred only)":
        print(
            "\n  This site publishes neither /.well-known/considerate.json nor a "
            "robots.txt Crawl-delay.\n  considerate inferred a starting tier from this "
            "one request's headers/latency instead — see SPEC.md §3.\n  Tell the site "
            "operator to publish examples/site_setup/considerate.json to get an explicit rate."
        )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    client = _client(args)
    host = httpx.URL(args.url).host
    print(f"considerate probe — {args.url}  ({args.requests} requests)")
    print(f"{'t':>6}  {'#':>3}  {'result':<28}  {'rate (req/s)':<13}  events")

    start = time.monotonic()
    events_log: list[str] = []
    client.on_event = lambda e: events_log.append(e.kind)

    for i in range(1, args.requests + 1):
        events_log.clear()
        t0 = time.monotonic()
        try:
            response = client.get(args.url)
            result = f"HTTP {response.status_code} ({(time.monotonic() - t0) * 1000:.0f}ms)"
        except DisallowedError:
            result = "DISALLOWED by robots.txt"
        except CircuitOpenError as e:
            result = f"CIRCUIT OPEN ({e.reason}, retry {e.retry_after:.0f}s)"
        except httpx.HTTPError as e:
            result = f"error: {type(e).__name__}"

        state = client._domains.get(host)
        rate = f"{state.controller.rate:.3f}" if state else "-"
        elapsed = time.monotonic() - start
        print(f"{elapsed:>5.1f}s  {i:>3}  {result:<28}  {rate:<13}  {', '.join(events_log) or '-'}")

        if "circuit_open" in events_log:
            wait = state.breaker.cooldown if state else 1.0
            print(f"        -> circuit open; waiting {wait:.0f}s cooldown before continuing probe")
            time.sleep(wait + 0.1)

    if not args.json:
        report = _domain_report(client, host)
        print("\nfinal state:")
        for k, v in report.items():
            print(f"  {k}: {v}")
    else:
        print(json.dumps(_domain_report(client, host), indent=2))

    client.close()
    return 0


def _add_common_args(p: argparse.ArgumentParser) -> None:
    # These live on each subparser, not the top-level parser: argparse
    # merges a subparser's whole namespace (including its own defaults)
    # back onto the parent's when a subcommand runs, which would silently
    # clobber a same-named flag given *before* the subcommand. Keeping them
    # subcommand-only avoids that footgun — pass them after the URL, e.g.
    # `considerate inspect URL --contact ...`.
    p.add_argument("--agent-name", default="considerate-cli", help="Identity sent in the Considerate-Agent header")
    p.add_argument("--contact", default=None, help="Contact URL/email sent in the Considerate-Agent header")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="considerate",
        description="Inspect and test the considerate protocol/library against a real URL.",
    )
    parser.add_argument("--version", action="version", version=f"considerate {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="One request: report what policy/tier was discovered")
    p_inspect.add_argument("url")
    _add_common_args(p_inspect)

    p_probe = sub.add_parser("probe", help="Several requests: watch AIMD/circuit-breaker react live")
    p_probe.add_argument("url")
    p_probe.add_argument("-n", "--requests", type=int, default=8, help="Number of requests to send (default: 8)")
    _add_common_args(p_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "probe":
        return cmd_probe(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
