"""Agent-side half of the handshake: who is making this request, and why.

considerate sends a `Considerate-Agent` request header on every request it
makes. It costs the agent nothing, and it's what lets a site's policy file
give a specific, known agent a different (often higher) rate than the
anonymous default — see SPEC.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_TOKEN_SAFE = "".maketrans({'"': "'", "\\": "/", "\n": " ", "\r": " ", ";": ","})


def _quote(value: str) -> str:
    return '"' + value.translate(_TOKEN_SAFE) + '"'


@dataclass(frozen=True)
class AgentIdentity:
    """Describes the agent for the purposes of the Considerate-Agent header.

    Attributes:
        name: Short, stable identifier for the agent/bot, e.g. "MyResearchBot".
            This is the key a site's policy file matches against in its
            `agents` map, so keep it consistent across versions.
        version: Free-form version string, e.g. "1.4.0".
        contact: A mailto: or https:// URL a site operator can use to reach
            a human about this agent's traffic. Strongly recommended — this
            is the single most useful field for turning "please stop" into
            an email instead of an abuse report.
        intent: One of the informal intent labels from SPEC.md
            ("browse", "bulk-scrape", "monitor", "research"), or any custom
            string. Purely informational; considerate does not enforce it.
        extra: Additional free-form key/value pairs to include in the header.
    """

    name: str
    version: str = "0"
    contact: str | None = None
    intent: str = "browse"
    extra: dict[str, str] = field(default_factory=dict)

    def to_header(self) -> str:
        """Render this identity as a Considerate-Agent header value.

        Format is deliberately simple `key="value"` pairs (a subset of RFC
        8941 Structured Field syntax) rather than a bespoke grammar, so it
        stays trivial to parse from any language without a library.
        """
        parts = [
            f"name={_quote(self.name)}",
            f"version={_quote(self.version)}",
            f"intent={_quote(self.intent)}",
        ]
        if self.contact:
            parts.append(f"contact={_quote(self.contact)}")
        for key, value in self.extra.items():
            parts.append(f"{key}={_quote(value)}")
        return ", ".join(parts)

    @property
    def user_agent_suffix(self) -> str:
        """A short suffix suitable for appending to a User-Agent string."""
        return f"{self.name}/{self.version}"


def parse_header(value: str) -> dict[str, str]:
    """Parse a Considerate-Agent header value back into a dict.

    Best-effort parser for site operators / middleware that want to read
    the header rather than build one. Not used by the client itself.
    """
    result: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, raw = part.partition("=")
        result[key.strip()] = raw.strip().strip('"')
    return result
