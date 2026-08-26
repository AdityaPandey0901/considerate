"""Shared structural types (`typing.Protocol`), so `_SharedLogic` methods
that only ever read `.status_code`/`.headers` off a response can be typed
against a small structural interface instead of a concrete `httpx.Response`
— every integration (httpx, requests, the Playwright adapter) hands one of
these methods a different concrete response type, and none of them need to
be an actual httpx.Response to satisfy what these methods actually do.
"""

from __future__ import annotations

from typing import Any, Protocol


class ResponseLike(Protocol):
    """What `_domain.apply_inference` and `_SharedLogic._record_outcome`
    actually need from a response: a status code, and a headers object
    supporting `.get(key, default)` / `in`. Every integration hands these
    methods a different concrete type (`httpx.Headers`,
    `requests.structures.CaseInsensitiveDict`, a plain `dict` from the
    Playwright adapter) — all satisfy that contract at runtime, but their
    stubs' `.get()` overloads aren't uniformly compatible with a single
    strict Protocol shape, so `headers` is intentionally left as `Any`
    rather than chasing exact structural typing across three libraries'
    slightly different stubs for the same duck-typed behavior.
    """

    status_code: int
    headers: Any
