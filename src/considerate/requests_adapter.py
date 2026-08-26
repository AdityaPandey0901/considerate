"""A `requests`-native integration: a `Transport Adapter
<https://requests.readthedocs.io/en/latest/user/advanced/#transport-adapters>`_
you mount onto an existing `requests.Session`, rather than a parallel client
class — the spec's own MVP scope calls for "a near-zero-effort swap for
existing scrapers," and for code already built on `requests`, mounting an
adapter *is* zero-effort: no call sites change.

    import requests
    from considerate import AgentIdentity
    from considerate.requests_adapter import ConsiderateAdapter

    session = requests.Session()
    session.mount("https://", ConsiderateAdapter(identity=AgentIdentity(name="MyBot")))
    session.mount("http://", ConsiderateAdapter(identity=AgentIdentity(name="MyBot")))

    response = session.get("https://example.com/page")  # metered, same as ConsiderateClient

Requires the optional `requests` dependency: `pip install considerate[requests]`.

Redirects need no special handling here (unlike the httpx-based
`ConsiderateClient` — see `_redirects.py`): `requests.Session` already
resolves each redirect hop via its own repeated call into
`get_adapter(url).send()`, so every hop naturally re-enters this adapter
and gets its own domain metering for free.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import OrderedDict
from typing import Any

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError as exc:  # pragma: no cover - exercised via the error message
    raise ImportError(
        "considerate.requests_adapter requires the 'requests' package: pip install considerate[requests]"
    ) from exc

from ._cache import PersistentPolicyCache, load_fresh
from ._domain import DomainState
from .client import _SharedLogic
from .config import ConsiderateConfig
from .events import EventCallback, emit
from .exceptions import CircuitOpenError, DisallowedError
from .identity import AgentIdentity

_DEFAULT_IDENTITY = AgentIdentity(name="considerate-agent", version="0", intent="browse")
_META_FETCH_TIMEOUT = 3.0


def _classify_requests_failure(exc: Exception) -> str:
    """Same stable reason vocabulary as `_util.classify_transport_failure`,
    reimplemented against `requests`' own exception hierarchy rather than
    httpx's — kept local to this module since `_util.py` is a core module
    that must import cleanly without the optional `requests` dependency.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        cause = exc.__cause__
        text = f"{exc} {cause}".lower()
        if isinstance(cause, socket.gaierror) or "getaddrinfo" in text or "name or service not known" in text:
            return "dns_error"
        return "connection_error"
    return "transport_error"


class ConsiderateAdapter(HTTPAdapter, _SharedLogic):  # type: ignore[misc]
    # The ignore above is for a genuine, irreducible static-typing artifact
    # of this dual inheritance: HTTPAdapter declares `self.config: dict`
    # and `_SharedLogic` declares `self.config: ConsiderateConfig` — mypy
    # correctly flags that as incompatible. At runtime it's a non-issue:
    # this class never uses `_SharedLogic`'s `config` (see the note above
    # `_new_domain_state`/`_touch_domain` below) — it overrides every
    # method that would touch it, using `considerate_config` instead.
    def __init__(
        self,
        identity: AgentIdentity | None = None,
        config: ConsiderateConfig | None = None,
        on_event: EventCallback | None = None,
        verified_identity: str | None = None,
        **adapter_kwargs: Any,
    ) -> None:
        self.identity = identity or _DEFAULT_IDENTITY
        self.considerate_config = config or ConsiderateConfig()
        self.on_event = on_event
        self.verified_identity = verified_identity
        self._domains: OrderedDict[str, DomainState] = OrderedDict()
        self._domains_lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}
        # A plain, unmounted session for /.well-known + robots.txt fetches —
        # deliberately never routed back through this adapter, or every
        # policy-discovery request would recursively try to discover its
        # own policy.
        self._meta_session = requests.Session()
        path = self.considerate_config.policy_cache_path
        self._persistent_cache = PersistentPolicyCache(path) if path else None
        super().__init__(**adapter_kwargs)

    def close(self) -> None:
        if self._persistent_cache is not None:
            self._persistent_cache.close()
        super().close()

    def snapshot(self) -> dict[str, dict]:
        """See `ConsiderateClient.snapshot()`."""
        with self._domains_lock:
            return {host: self._domain_snapshot(state) for host, state in self._domains.items()}

    # -- shared-logic plumbing --------------------------------------------
    # _new_domain_state/_touch_domain are overridden (not inherited from
    # _SharedLogic) because they read `self.config`, and HTTPAdapter.__init__
    # unconditionally sets `self.config = {}` for its own urllib3 pool/proxy
    # bookkeeping — a real attribute-name collision, not just a style
    # choice, which is why this class stores its own config as
    # `considerate_config` instead. _parse_well_known_response,
    # _parse_robots_response, and _record_outcome don't touch `self.config`
    # so those three are still reused as-is from the mixin.

    def _new_domain_state(self, host: str) -> DomainState:
        return DomainState(
            host=host,
            config=self.considerate_config,
            agent_name=self.identity.name,
            verified_identity=self.verified_identity,
        )

    def _touch_domain(self, host: str, state: DomainState) -> DomainState:
        self._domains[host] = state
        self._domains.move_to_end(host)
        while len(self._domains) > self.considerate_config.max_tracked_domains:
            self._domains.popitem(last=False)
        return state

    def _get_domain(self, host: str) -> DomainState:
        with self._domains_lock:
            state = self._domains.get(host)
            if state is None:
                state = self._new_domain_state(host)
            return self._touch_domain(host, state)

    def _get_semaphore(self, state: DomainState) -> threading.Semaphore:
        if self._semaphore_limits.get(state.host) != state.max_concurrent:
            self._semaphores[state.host] = threading.Semaphore(state.max_concurrent)
            self._semaphore_limits[state.host] = state.max_concurrent
        return self._semaphores[state.host]

    def _fetch_meta(self, host: str, path: str) -> tuple[int, str] | None:
        url = f"https://{host}{path}"
        try:
            with self._meta_session.get(url, timeout=_META_FETCH_TIMEOUT, stream=True) as resp:
                if resp.status_code != 200:
                    return resp.status_code, ""
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    total += len(chunk)
                    if total > self.considerate_config.meta_fetch_max_bytes:
                        return None
                    chunks.append(chunk)
                return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")
        except requests.RequestException:
            return None

    def _ensure_policy(self, state: DomainState, host: str) -> None:
        if state.policy_is_fresh():
            return

        well_known_policy = None
        used_disk_cache = False
        if self._persistent_cache is not None:
            disk_policy = load_fresh(self._persistent_cache, host, self.considerate_config.policy_cache_ttl)
            if disk_policy is not None:
                well_known_policy = disk_policy
                used_disk_cache = True

        if not used_disk_cache and self.considerate_config.fetch_well_known:
            fetched = self._fetch_meta(host, "/.well-known/considerate.json")
            if fetched:
                well_known_policy = self._parse_well_known_response(host, *fetched)

        if self.considerate_config.respect_robots_txt:
            fetched = self._fetch_meta(host, "/robots.txt")
            if fetched:
                crawl_policy, parser = self._parse_robots_response(*fetched)
                state.robots_parser = parser
                if well_known_policy is None:
                    well_known_policy = crawl_policy
            else:
                state.robots_parser = None

        if well_known_policy is not None:
            state.apply_policy(well_known_policy)
            if not used_disk_cache and self._persistent_cache is not None and well_known_policy.source == "well-known":
                self._persistent_cache.set(host, well_known_policy, time.time())
            label = well_known_policy.source + (" (disk cache)" if used_disk_cache else "")
            emit(self.on_event, "policy_discovered", host, source=label)
        else:
            state.policy_fetched_at = time.monotonic()

    # -- the actual Transport Adapter hook -----------------------------------

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        # PreparedRequest.url is typed as `str | bytes | None` (it can hold
        # bytes internally); in every path considerate's own code takes,
        # it's already a plain str by the time send() runs.
        assert isinstance(request.url, str), f"expected a str URL, got {type(request.url).__name__}"
        url: str = request.url

        host = requests.utils.urlparse(url).hostname
        assert host is not None, f"could not determine a host from {url!r}"
        state = self._get_domain(host)

        self._ensure_policy(state, host)

        robots_parser = state.robots_parser
        if self.considerate_config.respect_robots_txt and robots_parser is not None:
            if not robots_parser.can_fetch(self.identity.name, url):
                emit(self.on_event, "disallowed", host, url=url)
                raise DisallowedError(url)
        if state.is_path_disallowed(requests.utils.urlparse(url).path):
            emit(self.on_event, "disallowed", host, url=url, source="considerate.json disallow_paths")
            raise DisallowedError(url)

        state.refresh_effective_ceiling()
        allowed, retry_after = state.breaker.check()
        if not allowed:
            emit(self.on_event, "circuit_open", host, reason=state.breaker.reason, retry_after=retry_after)
            raise CircuitOpenError(host, state.breaker.reason, retry_after)

        with self._get_semaphore(state):
            wait = max(state.controller.wait_time(), state.retry_wait_remaining())
            if wait > 0:
                time.sleep(wait)
            state.controller.consume()

            request.headers.setdefault("Considerate-Agent", self.identity.to_header())

            start = time.monotonic()
            try:
                response = super().send(
                    request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies
                )
            except requests.exceptions.RequestException as exc:
                state.controller.report_failure()
                state.breaker.report_failure(_classify_requests_failure(exc))
                raise

            latency = time.monotonic() - start
            self._record_outcome(state, host, response, latency)  # also parses Retry-After (see client.py)
            return response
