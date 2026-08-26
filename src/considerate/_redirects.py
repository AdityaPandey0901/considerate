"""Redirect handling, factored out so it's identical for sync and async.

httpx's own `follow_redirects=True` chases a redirect chain *inside a
single call*, entirely invisible to per-domain metering — a redirect from
a fragile site to another fragile site would get zero rate-limiting or
policy discovery on the second hop. considerate always tells httpx
`follow_redirects=False` and re-enters its own `request()` for each hop
instead, so every hop gets its own domain state, its own policy discovery,
its own rate limit.
"""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

_SEE_OTHER_LIKE = (301, 302, 303)


def next_hop(method: str, status_code: int, url: str, location: str, kwargs: dict) -> tuple[str, str, dict]:
    """Compute the method/url/kwargs for the next hop of a redirect chain,
    mirroring the handful of rules that matter in practice (RFC 9110 §15.4):
    a 303 (and a 301/302 historically) downgrades a POST to a GET and drops
    the body; a 307/308 preserves method and body exactly.
    """
    next_url = urljoin(url, location)
    next_method = method
    next_kwargs = dict(kwargs)

    if status_code == 303 and method != "HEAD":
        next_method = "GET"
    elif status_code in (301, 302) and method == "POST":
        next_method = "GET"

    if next_method != method:
        for body_key in ("content", "data", "json", "files"):
            next_kwargs.pop(body_key, None)

    return next_method, next_url, next_kwargs


def is_redirect(response: httpx.Response) -> bool:
    return response.has_redirect_location
