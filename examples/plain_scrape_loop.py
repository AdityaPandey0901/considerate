"""The simplest possible "agent given an open-ended scrape task" scenario:
a loop over a list of URLs with no rate-limiting logic written by hand.

Run: python examples/plain_scrape_loop.py
"""

from __future__ import annotations

from considerate import AgentIdentity, CircuitOpenError, ConsiderateClient, DisallowedError


def on_event(event) -> None:
    # Wire this to your logger / agent's reporting channel. This is what
    # lets an agent say "I slowed down because..." instead of it being
    # invisible to whoever is watching the run.
    print(f"[considerate] {event.kind} domain={event.domain} {event.data}")


def main() -> None:
    identity = AgentIdentity(
        name="ExampleScraperAgent",
        version="1.0",
        contact="mailto:you@example.com",
        intent="bulk-scrape",
    )

    urls = [f"https://example.com/documents/{i}" for i in range(1, 401)]

    with ConsiderateClient(identity=identity, on_event=on_event) as client:
        fetched, skipped, paused = 0, 0, 0
        for url in urls:
            try:
                response = client.get(url)
                fetched += 1
                _ = response  # ... save/process it
            except DisallowedError:
                skipped += 1
                continue
            except CircuitOpenError as e:
                paused += 1
                print(f"Stopping early on this task: {e.payload}")
                break

        print(f"fetched={fetched} skipped={skipped} paused_and_stopped={paused}")


if __name__ == "__main__":
    main()
