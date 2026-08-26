"""considerate — a respectful rate-limiting layer for agents that fetch the web.

    from considerate import ConsiderateClient, AgentIdentity

    client = ConsiderateClient(
        identity=AgentIdentity(name="MyResearchBot", contact="mailto:me@example.com", intent="bulk-scrape")
    )
    response = client.get("https://example.com/page")

See SPEC.md for the wire protocol (the `Considerate-Agent` request header and
the `/.well-known/considerate.json` site policy file) and README.md for the
full quickstart.
"""

from ._version import __version__
from .breaker import BreakerConfig, BreakerState, CircuitBreaker
from .client import AsyncConsiderateClient, ConsiderateClient
from .config import ConsiderateConfig
from .controller import AimdController, ControllerConfig
from .events import Event
from .exceptions import CircuitOpenError, ConsiderateError, DisallowedError, PolicyError
from .identity import AgentIdentity
from .policy import RateRule, SitePolicy

__all__ = [
    "__version__",
    "AgentIdentity",
    "AimdController",
    "AsyncConsiderateClient",
    "BreakerConfig",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "ConsiderateClient",
    "ConsiderateConfig",
    "ConsiderateError",
    "ControllerConfig",
    "DisallowedError",
    "Event",
    "PolicyError",
    "RateRule",
    "SitePolicy",
]
