"""Header forwarding policy.

A **denylist**, deliberately. Anything not explicitly dangerous is forwarded, so
headers the provider invents after this code is written — a new ``x-ratelimit-*``
dimension, a new ``openai-*`` hint — keep reaching the caller with no change here.
An allowlist would silently swallow them, and the failure would be invisible.

Two classes of header must never be relayed:

* **Hop-by-hop headers** (RFC 9110 §7.6.1) describe a single connection, not the
  message. Relaying ``Connection`` or ``Transfer-Encoding`` onto a different
  connection is a protocol violation.
* **Headers that describe a body we changed.** httpx decodes ``Content-Encoding``
  for us and the body length can differ from upstream's, so both ``Content-Length``
  and ``Content-Encoding`` are recomputed by the server rather than forwarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# RFC 9110 §7.6.1. `Proxy-Authorization` is included: it authenticates the caller
# to *this* hop and must not be leaked to the provider.
HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# `host` is rewritten for the upstream origin; `content-length` is recomputed by
# httpx; `expect: 100-continue` is a hop-level negotiation we do not relay.
_REQUEST_STRIP: Final[frozenset[str]] = HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "expect",
}

# `date` and `server` are emitted by our own ASGI server; forwarding upstream's
# too would put two of each on the wire.
_RESPONSE_STRIP: Final[frozenset[str]] = HOP_BY_HOP_HEADERS | {
    "content-length",
    "content-encoding",
    "date",
    "server",
}


def filter_request_headers(headers: httpx.Headers | Mapping[str, str]) -> httpx.Headers:
    """Headers to send upstream.

    Returns ``httpx.Headers`` rather than a dict so that repeated headers survive.
    A client may legitimately send ``Accept`` twice, and collapsing the pair into
    one would change the request the provider sees.

    ``Authorization`` is forwarded untouched — the caller's key reaches the
    provider, and the gateway never needs to hold a credential of its own.
    """
    source = headers if isinstance(headers, httpx.Headers) else httpx.Headers(headers)
    return httpx.Headers(
        [(key, value) for key, value in source.multi_items() if key not in _REQUEST_STRIP]
    )


def filter_response_headers(headers: httpx.Headers) -> Iterator[tuple[str, str]]:
    """Headers to relay back to the caller.

    Yields pairs rather than a dict so repeated headers (``set-cookie``) survive.
    Preserves ``x-request-id``, every ``x-ratelimit-*``, ``retry-after``, and the
    ``openai-*`` family, all of which SDKs read for retry and diagnostics.
    """
    for key, value in headers.multi_items():
        if key.lower() not in _RESPONSE_STRIP:
            yield key, value
