<!--
DRAFT ONLY — not posted to github.com/crewAIInc/crewAI.
Intended as the body of a GitHub issue/discussion.
-->

## Idea: politeness/rate-limiting for scraping tools used by crews

A crew given an open-ended research/scraping task (e.g. via a custom tool
wrapping `requests`) has no default protection against overloading a
small, self-hosted site — nothing paces requests per-domain or reacts to
a `503` by slowing down or stopping.

I built [`considerate`](https://github.com/AdityaPandey0901/considerate)
to address exactly this for other frameworks: an adaptive (AIMD) rate
limiter + circuit breaker, with a `requests` Transport Adapter (mount it
on an existing `Session`, zero call-site changes) and a working example
tool ([`examples/crewai_tool_example.py`](https://github.com/AdityaPandey0901/considerate/blob/main/examples/crewai_tool_example.py))
using the `@tool` decorator.

Given CrewAI already ships a fairly large first-party tools ecosystem
(`crewai-tools`), would a `ConsiderateScrapeTool` (or similar) be a
reasonable addition there, or is this better left as a documented pattern
for people building custom tools? Happy to put together a PR either way,
mostly checking direction first — the underlying behavior contract is
small enough to describe independent of any specific library:
https://github.com/AdityaPandey0901/considerate/blob/main/SPEC.md#3-the-agent-identity-header
