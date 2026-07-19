"""The upstream client verifies TLS against the OS trust store.

The failure this guards against: a locally installed CA — from a corporate proxy or
antivirus HTTPS inspection (e.g. Avast, Zscaler) — re-signs upstream certificates. That CA
lives in the OS trust store, not in certifi's bundle, so a certifi-only client rejects every
HTTPS connection ("unable to get local issuer certificate") and the gateway 502s on every
request while the assistant itself works. Trusting the OS store is what fixes it.
"""

from __future__ import annotations

import asyncio
import ssl

from gateway.main import _upstream_ssl_context, build_upstream_client
from tests.conftest import build_settings


def test_upstream_ssl_context_uses_the_os_trust_store() -> None:
    ctx = _upstream_ssl_context()
    # truststore.SSLContext subclasses ssl.SSLContext and delegates verification to the OS
    # store, so a locally installed inspection CA is trusted the same way the OS trusts it.
    assert isinstance(ctx, ssl.SSLContext)
    assert type(ctx).__module__.startswith("truststore")


def test_build_upstream_client_accepts_the_os_trust_store_context() -> None:
    # The client builds with the OS-trust-store context and closes cleanly — i.e. httpx
    # accepts the custom SSL context on this platform.
    client = build_upstream_client(build_settings())
    asyncio.run(client.aclose())
