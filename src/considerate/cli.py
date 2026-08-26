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
from pathlib import Path

import httpx

from ._version import __version__
from .client import ConsiderateClient
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity
from .policy import PolicyError, parse_well_known


def _client(args: argparse.Namespace) -> ConsiderateClient:
    identity = AgentIdentity(
        name=args.agent_name,
        version=__version__,
        contact=args.contact,
        intent="research" if args.command == "inspect" else "monitor",
    )
    return ConsiderateClient(identity=identity, timeout=10.0)


def _domain_report(client: ConsiderateClient, host: str) -> dict:
    # Built on the same public `snapshot()` any caller can use for
    # monitoring (C3) — the CLI is just one consumer of it, not a special
    # path with its own state-reading logic.
    s = client.snapshot()[host]
    return {
        "domain": s["domain"],
        "policy_source": s["policy_source"] or "none (inferred only)",
        "policy_contact": s["policy_contact"],
        "hard_ceiling": s["hard_ceiling"],
        "current_rate_req_per_s": s["current_rate_req_per_s"],
        "ceiling_req_per_s": s["effective_ceiling_req_per_s"],
        "max_concurrent": s["max_concurrent"],
        "declared_rate_for_this_agent": s["declared_rate_for_identity"],
        "circuit_state": s["circuit_state"],
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


def _parse_duration(text: str) -> float:
    """"30s" / "2m" / "1h" / a bare number of seconds -> float seconds."""
    text = text.strip().lower()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "ms": 0.001}
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def cmd_probe(args: argparse.Namespace) -> int:
    client = _client(args)
    host = httpx.URL(args.url).host

    duration = _parse_duration(args.duration) if args.duration else None
    plan = f"for {args.duration}" if duration else f"({args.requests} requests)"
    print(f"considerate probe — {args.url}  {plan}")
    print(f"{'t':>6}  {'#':>3}  {'result':<28}  {'rate (req/s)':<13}  events")

    out_fh = open(args.out, "a") if args.out else None

    start = time.monotonic()
    events_log: list[str] = []
    client.on_event = lambda e: events_log.append(e.kind)

    i = 0
    while True:
        i += 1
        elapsed_before = time.monotonic() - start
        if duration is not None:
            if elapsed_before >= duration:
                i -= 1
                break
        elif i > args.requests:
            i -= 1
            break

        events_log.clear()
        t0 = time.monotonic()
        status_code = None
        try:
            response = client.get(args.url)
            status_code = response.status_code
            result = f"HTTP {response.status_code} ({(time.monotonic() - t0) * 1000:.0f}ms)"
        except DisallowedError:
            result = "DISALLOWED by robots.txt"
        except CircuitOpenError as e:
            result = f"CIRCUIT OPEN ({e.reason}, retry {e.retry_after:.0f}s)"
        except httpx.HTTPError as e:
            result = f"error: {type(e).__name__}"

        state = client._domains.get(host)
        rate = state.controller.rate if state else None
        elapsed = time.monotonic() - start
        print(f"{elapsed:>5.1f}s  {i:>3}  {result:<28}  {rate if rate is None else f'{rate:.3f}':<13}  {', '.join(events_log) or '-'}")

        if out_fh:
            out_fh.write(
                json.dumps(
                    {
                        "t": round(elapsed, 3),
                        "request": i,
                        "status_code": status_code,
                        "result": result,
                        "rate_req_per_s": rate,
                        "events": list(events_log),
                    }
                )
                + "\n"
            )
            out_fh.flush()

        if "circuit_open" in events_log:
            wait = state.breaker.cooldown if state else 1.0
            print(f"        -> circuit open; waiting {wait:.0f}s cooldown before continuing probe")
            time.sleep(wait + 0.1)

    if out_fh:
        out_fh.close()
        print(f"\nwrote {i} line(s) of NDJSON to {args.out}")

    if not args.json:
        report = _domain_report(client, host)
        print("\nfinal state:")
        for k, v in report.items():
            print(f"  {k}: {v}")
    else:
        print(json.dumps(_domain_report(client, host), indent=2))

    client.close()
    return 0


def _load_schema() -> dict:
    import importlib.resources

    text = (importlib.resources.files("considerate") / "schema" / "considerate.schema.json").read_text()
    return json.loads(text)


def cmd_policy_validate(args: argparse.Namespace) -> int:
    try:
        import jsonschema
    except ImportError:
        print("`considerate policy validate` requires jsonschema: pip install considerate[validate]")
        return 1

    target = args.path_or_url
    if target.startswith("http://") or target.startswith("https://"):
        try:
            text = httpx.get(target, timeout=10.0, follow_redirects=True).text
        except httpx.HTTPError as e:
            print(f"Could not fetch {target}: {e}")
            return 1
    else:
        try:
            text = Path(target).read_text()
        except OSError as e:
            print(f"Could not read {target}: {e}")
            return 1

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"{target}: invalid JSON — {e}")
        return 1

    validator = jsonschema.Draft202012Validator(_load_schema())
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        print(f"{target}: {len(errors)} schema error(s)")
        for err in errors:
            loc = "/".join(str(p) for p in err.path) or "(root)"
            print(f"  - {loc}: {err.message}")
        return 1

    try:
        policy = parse_well_known(text)
    except PolicyError as e:
        print(f"{target}: schema-valid but failed to parse — {e}")
        return 1

    print(f"{target}: valid")
    print(f"  version: {policy.version}")
    print(f"  default rate: {policy.default.requests_per_second} req/s" if policy.default.requests_per_second is not None else "  default rate: (not set)")
    if policy.agents:
        print(f"  named agent overrides: {', '.join(policy.agents)}")
    if policy.verified_agents:
        print(f"  verified agent overrides: {', '.join(policy.verified_agents)}")
    if policy.disallow_paths:
        print(f"  disallow_paths: {policy.disallow_paths}")
    if policy.crawl_windows:
        print(f"  crawl_windows: {len(policy.crawl_windows)} defined")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    def ask(prompt: str, default: str) -> str:
        if args.yes:
            return default
        raw = input(f"{prompt} [{default}]: ").strip()
        return raw or default

    contact = ask("Contact (mailto: or https:)", "mailto:ops@example.com")
    rate_str = ask("Default requests/second for unrecognized agents", "0.5")
    concurrent_str = ask("Max concurrent requests", "1")
    rate = float(rate_str)
    concurrent = int(concurrent_str)

    doc = {
        "version": "0.2",
        "contact": contact,
        "default": {"requests_per_second": rate, "max_concurrent": concurrent},
    }
    out_path = Path(args.output)
    out_path.write_text(json.dumps(doc, indent=2) + "\n")

    crawl_delay = max(1, round(1.0 / rate)) if rate > 0 else 60
    print(f"Wrote {out_path}")
    print(f"\nPublish it at https://your-domain.example/.well-known/considerate.json")
    print("\nAs a fallback for agents that don't check that file yet, add this to robots.txt:\n")
    print(f"User-agent: *\nCrawl-delay: {crawl_delay}")
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
    p_probe.add_argument("-d", "--duration", default=None, help='Run for this long instead of a fixed count, e.g. "30s", "2m" (overrides -n)')
    p_probe.add_argument("-o", "--out", default=None, help="Append each request as an NDJSON line to this file")
    _add_common_args(p_probe)

    p_policy = sub.add_parser("policy", help="Site-policy-file tooling")
    policy_sub = p_policy.add_subparsers(dest="policy_command", required=True)
    p_validate = policy_sub.add_parser("validate", help="Validate a considerate.json file or URL against the schema")
    p_validate.add_argument("path_or_url")

    p_init = sub.add_parser("init", help="Scaffold a considerate.json for your own site")
    p_init.add_argument("-o", "--output", default="considerate.json", help="Where to write the file (default: ./considerate.json)")
    p_init.add_argument("-y", "--yes", action="store_true", help="Accept defaults without prompting")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "policy" and args.policy_command == "validate":
        return cmd_policy_validate(args)
    if args.command == "init":
        return cmd_init(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
